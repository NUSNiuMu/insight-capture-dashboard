from collections import deque
import sys
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from insight_capture.runtime.diagnostics import DiagnosticEvents, input_snapshot
from insight_capture.runtime.watchdog import ParticipantWatchdog


class ParticipantWatchdogTest(unittest.TestCase):
    def setUp(self):
        self.logger = Mock()
        self.owner = SimpleNamespace(
            _playback_mode=False, cameras=[SimpleNamespace(name="a"), SimpleNamespace(name="b")],
            camera_stale_timeout_sec=2., camera_input_lock=threading.Lock(),
            camera_input_times={"a": deque([9.9]), "b": deque()},
            camera_liveness_times={"a": 9.9, "b": 0.},
            last_pose_received_time={"a": 10., "b": 10.},
            events=DiagnosticEvents(self.logger))

    def test_global_pose_does_not_hide_missing_sdk_inputs(self):
        snapshot = input_snapshot(self.owner, 10.)
        self.assertEqual(snapshot["b"]["image_state"], "no_input")
        self.assertEqual(snapshot["b"]["vio_state"], "no_input")
        self.assertEqual(snapshot["a"]["image_state"], "receiving")

    def test_sdk_pose_does_not_hide_missing_image(self):
        self.owner.camera_liveness_times["b"] = 9.9
        snapshot = input_snapshot(self.owner, 10.)["b"]
        self.assertEqual(snapshot["image_state"], "no_input")
        self.assertEqual(snapshot["vio_state"], "receiving")

    def test_recovery_is_per_stream_and_repeated_state_is_silent(self):
        watchdog = ParticipantWatchdog(self.owner)
        with patch("insight_capture.runtime.diagnostics.time.monotonic", return_value=10.):
            watchdog.check_inputs()
            watchdog.check_inputs()
            self.assertEqual(len(self.owner.events.snapshot()), 4)
            self.owner.camera_input_times["b"].append(9.9)
            watchdog.check_inputs()
        self.assertEqual(len(self.owner.events.snapshot()), 5)
        event = self.owner.events.snapshot()[-1]
        self.assertEqual((event["component"], event["device"], event["state"]),
                         ("image_input", "b", "receiving"))

    def test_playback_does_not_report_camera_failures(self):
        self.owner._playback_mode = True
        ParticipantWatchdog(self.owner).check_inputs()
        self.assertEqual(self.owner.events.snapshot(), [])


if __name__ == "__main__":
    unittest.main()
