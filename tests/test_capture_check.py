import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from dashboard_runtime.capture_check import CaptureCheckManager, REQUIRED_ROLES  # noqa: E402
from dashboard_runtime.models import PoseSample  # noqa: E402


POSES = {
    "insight9_a": ("head", (0.0, 0.0, 1.4)),
    "insight3_a": ("right_hand", (0.35, 0.1, 1.0)),
    "insight3_b": ("left_hand", (-0.35, 0.1, 1.0)),
}


def mapping_snapshot():
    return {
        "map_point_count": 120,
        "statuses": {
            "insight9": {"online": True},
            "insight3_a": {
                "online": True,
                "localized": True,
                "tracking_mode": "map_matched",
            },
            "insight3_b": {
                "online": True,
                "localized": True,
                "tracking_mode": "map_matched",
            },
        },
    }


class CaptureCheckManagerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.manager = CaptureCheckManager(
            pose_roles={name: role for name, (role, _) in POSES.items()},
            mapping_snapshot=mapping_snapshot,
            results_root=Path(self.temporary.name),
            config={"minimum_samples": 8, "minimum_window_sec": 0.6},
        )

    def tearDown(self):
        self.temporary.cleanup()

    def feed(self, offsets=None, jitter=0.0):
        offsets = offsets or {}
        for samples in self.manager._samples.values():
            samples.clear()
        now = time.monotonic()
        for index in range(30):
            stamp = now - 0.95 + index * 0.03
            sign = -1.0 if index % 2 else 1.0
            for name, (_, base) in POSES.items():
                offset = offsets.get(name, (0.0, 0.0, 0.0))
                position = tuple(
                    base[axis] + offset[axis] + sign * jitter for axis in range(3)
                )
                self.manager.record_pose(
                    name,
                    PoseSample(position, (0.0, 0.0, 0.0, 1.0)),
                    stamp,
                )

    def test_reference_and_identical_check_pass(self):
        self.feed()
        self.assertEqual(self.manager.set_reference()["state"], "reference_saved")
        self.assertEqual(self.manager.reference["version"], 2)
        self.assertEqual(set(self.manager.reference["poses"]), set(REQUIRED_ROLES))
        self.feed()
        result = self.manager.check(bag_name="cup_stack_001")
        self.assertEqual(result["state"], "pass")
        self.assertTrue(
            (Path(self.temporary.name) / "capture_checks/cup_stack_001.json").is_file()
        )

    def test_moderate_offset_requests_reseat(self):
        self.feed()
        self.manager.set_reference()
        self.feed(offsets={"insight3_a": (0.02, 0.0, 0.0)})
        result = self.manager.check()
        self.assertEqual(result["state"], "retry")
        self.assertEqual(result["suspect_roles"], ["right_hand"])
        self.assertEqual(result["comparisons"]["insight3_a"]["threshold_group"], "insight3")

    def test_large_offset_requires_recalibration(self):
        self.feed()
        self.manager.set_reference()
        self.feed(offsets={"insight3_b": (0.05, 0.0, 0.0)})
        result = self.manager.check()
        self.assertEqual(result["state"], "recalibrate")
        self.assertEqual(result["suspect_cameras"], ["insight3_b"])

    def test_common_global_offset_is_not_hidden_by_relative_geometry(self):
        self.feed()
        self.manager.set_reference()
        common_offset = {name: (0.02, 0.0, 0.0) for name in POSES}
        self.feed(offsets=common_offset)
        result = self.manager.check()
        self.assertEqual(result["state"], "retry")
        self.assertCountEqual(
            result["suspect_cameras"], ["insight3_a", "insight3_b"]
        )
        self.assertEqual(result["comparisons"]["insight9_a"]["state"], "pass")

    def test_insight9_uses_looser_global_pose_thresholds(self):
        self.feed()
        self.manager.set_reference()
        self.feed(offsets={"insight9_a": (0.03, 0.0, 0.0)})
        self.assertEqual(self.manager.check()["state"], "pass")
        self.feed(offsets={"insight9_a": (0.05, 0.0, 0.0)})
        result = self.manager.check()
        self.assertEqual(result["state"], "retry")
        self.assertEqual(result["suspect_cameras"], ["insight9_a"])

    def test_motion_prevents_measurement(self):
        self.feed(jitter=0.012)
        result = self.manager.set_reference()
        self.assertEqual(result["state"], "not_ready")
        self.assertTrue(any("moving" in reason for reason in result["reasons"]))

    def test_check_without_reference_guides_setup(self):
        self.feed()
        self.assertEqual(self.manager.check()["state"], "no_reference")

    def test_global_localization_is_required_but_current_rematch_is_not(self):
        self.feed()
        localized_mapping = mapping_snapshot()
        localized_mapping["statuses"]["insight3_a"]["tracking_mode"] = "vio_only"
        self.manager.mapping_snapshot = lambda: localized_mapping
        self.assertEqual(self.manager.set_reference()["state"], "reference_saved")

        unlocalized_mapping = mapping_snapshot()
        unlocalized_mapping["statuses"]["insight3_a"]["localized"] = False
        self.manager.mapping_snapshot = lambda: unlocalized_mapping
        result = self.manager.set_reference()
        self.assertEqual(result["state"], "not_ready")
        self.assertTrue(any("globally localized" in item for item in result["reasons"]))


if __name__ == "__main__":
    unittest.main()
