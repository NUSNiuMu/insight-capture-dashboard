import os
import sys
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from insight_capture.runtime.preflight import CapturePreflight  # noqa: E402
from insight_capture.runtime.recording import (  # noqa: E402
    RecordingManager,
    build_vio_calibration_topics,
    vio_calibration_raw_image_topics,
)


class _Manager:
    def __init__(self, root: Path, topics: list[str]) -> None:
        self.rosbag_root = root
        self.default_topics = []
        self.storage_status = {"using_fallback": False, "active_path": str(root)}
        self._topics = topics

    def current_topic_catalog(self, refresh=True):
        return {"topics": list(self._topics)}


class VioCalibrationRecordingTest(unittest.TestCase):
    def test_topic_contract_is_exact_and_has_no_global_pose(self):
        topics = build_vio_calibration_topics()

        self.assertEqual(len(topics), 17)
        self.assertEqual(len(set(topics)), 17)
        self.assertEqual(len(vio_calibration_raw_image_topics()), 4)
        self.assertEqual(topics[-1], "/tf_static")
        self.assertFalse(any(topic.startswith("/insight_global/") for topic in topics))

    def test_calibration_preflight_skips_mapping_and_requires_all_16_live_topics(self):
        topics = build_vio_calibration_topics()
        now = time.monotonic()
        cameras = [
            SimpleNamespace(
                name=name,
                label=name,
                topic=f"/{name}/camera/infra1/image_rect_raw",
            )
            for name in ("insight3_a", "insight3_b", "insight9_a")
        ]
        node = SimpleNamespace(
            fake_pose=False,
            cameras=cameras,
            poses=[],
            camera_input_lock=threading.Lock(),
            camera_input_times={
                camera.name: (
                    deque(maxlen=10)
                    if camera.name.startswith("insight3")
                    else deque([now - 0.1, now], maxlen=10)
                )
                for camera in cameras
            },
            build_mapping_payload=lambda: (_ for _ in ()).throw(
                AssertionError("mapping must not be required")
            ),
        )
        manager = _Manager(Path(self.id()).parent, topics)
        manager.rosbag_root = ROOT
        preflight = CapturePreflight(node, manager, {"minimum_free_gb": 0})

        report = preflight.evaluate(topics, mode="vio_calibration")

        self.assertTrue(report["ok"], report)
        self.assertEqual(report["mode"], "vio_calibration")
        self.assertEqual(len(report["topics"]["required"]), 16)
        self.assertEqual(report["camera_health"], {})
        self.assertNotIn(
            "camera_stale", {item["code"] for item in report["failures"]}
        )
        self.assertIn("未校正图像", preflight.speech(report))

    def test_payload_probe_uses_dashboard_rmw_instead_of_recorder_override(self):
        manager = RecordingManager(
            {},
            20,
            ROOT,
            1024,
            [],
            recording_rmw_implementation="rmw_cyclonedds_cpp",
        )
        completed = SimpleNamespace(returncode=0, stderr="")
        with (
            mock.patch.dict(
                os.environ,
                {"RMW_IMPLEMENTATION": "rmw_cyclonedds_cpp"},
            ),
            mock.patch("subprocess.run", return_value=completed) as run,
        ):
            report = manager.probe_topic_payloads(["/camera/image_raw"])

        self.assertTrue(report["ok"])
        self.assertNotIn("RMW_IMPLEMENTATION", run.call_args.kwargs["env"])


if __name__ == "__main__":
    unittest.main()
