import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from insight_capture.postprocess.quality.station_check import (  # noqa: E402
    CaptureCheckManager,
    FIXED_STATION_ROLES,
)
from insight_capture.common.models import PoseSample  # noqa: E402


POSES = {
    "insight9_a": ("head", (0.0, 0.0, 1.4)),
    "insight3_a": ("right_hand", (0.35, 0.1, 1.0)),
    "insight3_b": ("left_hand", (-0.35, 0.1, 1.0)),
}


def validation_status(
    *, count=1, translation_error_m=0.005, rotation_error_deg=0.5
):
    return {
        "session_id": "test-session",
        "session_generation": 1,
        "reference_active": True,
        "reference_id": 4,
        "reference_keyframe": 80,
        "validation_count": count,
        "last_validation": (
            {
                "sequence": count,
                "translation_error_m": translation_error_m,
                "rotation_error_deg": rotation_error_deg,
                "descriptor_matches": 42,
                "inliers": 31,
                "inlier_ratio": 0.738,
                "median_reprojection_error_px": 0.72,
                "grid_cells": 9,
                "age_sec": 0.2,
            }
            if count > 0
            else None
        ),
        "recent_validations": (
            [
                {
                    "sequence": count,
                    "translation_error_m": translation_error_m,
                    "rotation_error_deg": rotation_error_deg,
                }
            ]
            if count > 0
            else []
        ),
    }


def mapping_snapshot():
    return {
        "map_point_count": 120,
        "statuses": {
            "insight9": {
                "online": True,
                "capture_validation": validation_status(),
            },
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
        self.mapping = mapping_snapshot()
        self.manager = CaptureCheckManager(
            pose_roles={name: role for name, (role, _) in POSES.items()},
            mapping_snapshot=lambda: self.mapping,
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

    def save_reference(self):
        self.feed()
        result = self.manager.set_reference(
            insight9_reference=validation_status(count=0)
        )
        self.assertEqual(result["state"], "reference_saved")

    def set_validation(self, **kwargs):
        self.mapping["statuses"]["insight9"]["capture_validation"] = (
            validation_status(**kwargs)
        )

    def test_reference_and_identical_check_pass(self):
        self.save_reference()
        self.assertEqual(self.manager.reference["version"], 3)
        self.assertEqual(
            set(self.manager.reference["poses"]), set(FIXED_STATION_ROLES)
        )
        self.feed()
        result = self.manager.check(bag_name="cup_stack_001")
        self.assertEqual(result["state"], "pass")
        self.assertTrue(
            (Path(self.temporary.name) / "capture_checks/cup_stack_001.json").is_file()
        )

    def test_moderate_offset_requests_reseat(self):
        self.save_reference()
        self.feed(offsets={"insight3_a": (0.02, 0.0, 0.0)})
        result = self.manager.check()
        self.assertEqual(result["state"], "retry")
        self.assertEqual(result["suspect_roles"], ["right_hand"])
        self.assertEqual(
            result["comparisons"]["insight3_a"]["threshold_group"], "insight3"
        )

    def test_large_offset_requires_recalibration(self):
        self.save_reference()
        self.feed(offsets={"insight3_b": (0.05, 0.0, 0.0)})
        result = self.manager.check()
        self.assertEqual(result["state"], "recalibrate")
        self.assertEqual(result["suspect_cameras"], ["insight3_b"])

    def test_common_global_offset_is_not_hidden_by_relative_geometry(self):
        self.save_reference()
        common_offset = {name: (0.02, 0.0, 0.0) for name in POSES}
        self.feed(offsets=common_offset)
        result = self.manager.check()
        self.assertEqual(result["state"], "retry")
        self.assertCountEqual(
            result["suspect_cameras"], ["insight3_a", "insight3_b"]
        )
        self.assertEqual(result["comparisons"]["insight9_a"]["state"], "pass")

    def test_insight9_uses_frozen_map_closure_instead_of_station_pose(self):
        self.save_reference()
        self.feed(offsets={"insight9_a": (3.0, -2.0, 1.0)})
        self.assertEqual(self.manager.check()["state"], "pass")

        self.set_validation(count=2, translation_error_m=0.05)
        self.feed(offsets={"insight9_a": (-4.0, 1.0, 0.5)})
        result = self.manager.check()
        self.assertEqual(result["state"], "retry")
        self.assertEqual(result["suspect_cameras"], ["insight9_a"])
        self.assertEqual(
            result["comparisons"]["insight9_a"]["method"],
            "frozen_natural_map_closure",
        )

    def test_fresh_insight9_closure_is_required_for_each_episode(self):
        self.save_reference()
        self.feed()
        self.assertEqual(self.manager.check()["state"], "pass")
        self.feed()
        result = self.manager.check()
        self.assertEqual(result["state"], "retry")
        self.assertIn(
            "no fresh Insight9 closure",
            result["comparisons"]["insight9_a"]["reason"],
        )

    def test_old_episode_closure_cannot_validate_the_boundary(self):
        self.save_reference()
        stale = validation_status(count=1)
        stale["last_validation"]["age_sec"] = 8.0
        self.mapping["statuses"]["insight9"]["capture_validation"] = stale
        self.feed()
        result = self.manager.check()
        self.assertEqual(result["state"], "retry")
        self.assertIn(
            "closure is stale",
            result["comparisons"]["insight9_a"]["reason"],
        )

    def test_map_session_change_requires_recalibration(self):
        self.save_reference()
        changed = validation_status(count=1)
        changed["session_generation"] = 2
        self.mapping["statuses"]["insight9"]["capture_validation"] = changed
        self.feed()
        result = self.manager.check()
        self.assertEqual(result["state"], "recalibrate")
        self.assertEqual(result["suspect_roles"], ["head"])

    def test_lost_frozen_reference_requires_recalibration(self):
        self.save_reference()
        reset = validation_status(count=0)
        reset["reference_active"] = False
        reset["session_generation"] = 2
        self.mapping["statuses"]["insight9"]["capture_validation"] = reset
        self.feed()
        result = self.manager.check()
        self.assertEqual(result["state"], "recalibrate")
        self.assertEqual(result["suspect_roles"], ["head"])

    def test_critical_closure_cannot_be_hidden_by_a_later_small_correction(self):
        self.save_reference()
        status = validation_status(count=2)
        status["recent_validations"] = [
            {
                "sequence": 1,
                "translation_error_m": 0.09,
                "rotation_error_deg": 1.0,
            },
            {
                "sequence": 2,
                "translation_error_m": 0.005,
                "rotation_error_deg": 0.5,
            },
        ]
        self.mapping["statuses"]["insight9"]["capture_validation"] = status
        self.feed()
        result = self.manager.check()
        self.assertEqual(result["state"], "recalibrate")
        self.assertAlmostEqual(
            result["comparisons"]["insight9_a"]["translation_error_m"], 0.09
        )

    def test_motion_prevents_measurement(self):
        self.feed(jitter=0.012)
        result = self.manager.set_reference(
            insight9_reference=validation_status(count=0)
        )
        self.assertEqual(result["state"], "not_ready")
        self.assertTrue(any("moving" in reason for reason in result["reasons"]))

    def test_check_without_reference_guides_setup(self):
        self.feed()
        self.assertEqual(self.manager.check()["state"], "no_reference")

    def test_global_localization_is_required_but_current_rematch_is_not(self):
        self.feed()
        localized_mapping = mapping_snapshot()
        localized_mapping["statuses"]["insight3_a"]["tracking_mode"] = "vio_only"
        self.mapping = localized_mapping
        result = self.manager.set_reference(
            insight9_reference=validation_status(count=0)
        )
        self.assertEqual(result["state"], "reference_saved")

        unlocalized_mapping = mapping_snapshot()
        unlocalized_mapping["statuses"]["insight3_a"]["localized"] = False
        self.mapping = unlocalized_mapping
        result = self.manager.set_reference(
            insight9_reference=validation_status(count=0)
        )
        self.assertEqual(result["state"], "not_ready")
        self.assertTrue(any("globally localized" in item for item in result["reasons"]))


if __name__ == "__main__":
    unittest.main()
