from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from dashboard_runtime.watchdog import ParticipantWatchdog  # noqa: E402


class _PlaybackOwner:
    _playback_mode = True

    def _restart_for_stale_participant(self, _reason: str) -> None:
        raise AssertionError("watchdog must remain suspended during playback")


class ParticipantWatchdogTest(unittest.TestCase):
    def test_prepared_playback_suspends_stale_participant_checks(self) -> None:
        watchdog = ParticipantWatchdog(_PlaybackOwner())
        with mock.patch(
            "dashboard_runtime.watchdog.time.sleep",
            side_effect=[None, StopIteration],
        ):
            with self.assertRaises(StopIteration):
                watchdog._stale_participant_watchdog_loop()


if __name__ == "__main__":
    unittest.main()
