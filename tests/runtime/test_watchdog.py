from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest import mock
from collections import deque


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from insight_capture.runtime.watchdog import ParticipantWatchdog  # noqa: E402


class _PlaybackOwner:
    _playback_mode = True

    def _restart_for_stale_participant(self, _reason: str) -> None:
        raise AssertionError("watchdog must remain suspended during playback")


class ParticipantWatchdogTest(unittest.TestCase):
    def test_capture_mode_raw_input_counts_as_ros_data(self) -> None:
        owner = mock.MagicMock()
        owner.camera_input_times = {
            "a": deque([1.0]),
            "b": deque(),
            "c": deque(),
        }
        owner.last_pose_received_time = {"a": 0.0, "b": 0.0, "c": 0.0}
        self.assertTrue(ParticipantWatchdog(owner)._any_ros_data_received())

    def test_native_vio_keeps_raw_only_calibration_camera_alive(self) -> None:
        owner = mock.MagicMock()
        owner.camera_input_times = {"insight3_a": deque([10.0])}
        owner.last_pose_received_time = {"insight3_a": 10.0}
        owner.camera_liveness_times = {"insight3_a": 40.0}

        watchdog = ParticipantWatchdog(owner)

        self.assertEqual(watchdog._camera_last_seen("insight3_a"), 40.0)

    def test_camera_is_stale_only_after_all_live_paths_stop(self) -> None:
        owner = mock.MagicMock()
        owner.camera_input_times = {"insight3_a": deque([10.0])}
        owner.last_pose_received_time = {"insight3_a": 20.0}
        owner.camera_liveness_times = {"insight3_a": 30.0}

        watchdog = ParticipantWatchdog(owner)

        self.assertEqual(watchdog._camera_last_seen("insight3_a"), 30.0)

    def test_prepared_playback_suspends_stale_participant_checks(self) -> None:
        watchdog = ParticipantWatchdog(_PlaybackOwner())
        with mock.patch(
            "insight_capture.runtime.watchdog.time.sleep",
            side_effect=[None, StopIteration],
        ):
            with self.assertRaises(StopIteration):
                watchdog._stale_participant_watchdog_loop()


if __name__ == "__main__":
    unittest.main()
