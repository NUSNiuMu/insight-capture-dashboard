import os
import threading
import time
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from deploy import power_button as button


def status(recording=False, merge="idle", take="complete", **extra):
    return {"recording": recording, "merge_state": merge, "current_take": {"state": take},
            "upgrade_lock_supported": True, **extra}


class ButtonChecks(unittest.TestCase):
    def check_capture(self, responses, expected_endpoints):
        with patch.object(button, "recording_request", side_effect=responses) as api, patch.object(button.time, "sleep"):
            button.wait_for_capture(time.monotonic() + 5)
        self.assertEqual([call.args[0] for call in api.call_args_list], expected_endpoints)

    def test_idle(self):
        self.check_capture([status()], ["status"])

    def test_stop_and_wait_for_save(self):
        self.check_capture(
            [status(True, take="recording"), status(False, "finalizing", "finalizing"),
             status(False, "finalizing", "finalizing", operation_in_progress=True),
             status(False, "done")],
            ["status", "stop", "status", "status"],
        )

    def test_existing_save_is_not_stopped_twice(self):
        self.check_capture([status(True, "finalizing", "finalizing"), status(False, "done")],
                           ["status", "status"])

    def test_distributed_tracks_both_saves(self):
        self.check_capture(
            [status(True, "finalizing", "finalizing",
                    distributed={"phase": "stop_pending", "local_saved": True, "remote_saved": False}),
             status(False, "saved", "saved",
                    distributed={"phase": "saved", "local_saved": True, "remote_saved": True})],
            ["status", "status"],
        )

    def test_save_error_keeps_device_online(self):
        with patch.object(button, "recording_request", return_value=status(False, "error", "finalization_failed")) as api:
            with self.assertRaisesRegex(RuntimeError, "finalization failed"):
                button.wait_for_capture(time.monotonic() + 5)
        api.assert_called_once_with("status")

    def test_dashboard_stopped_before_button_press(self):
        refused = URLError(ConnectionRefusedError())
        with patch.object(button, "recording_request", side_effect=refused), patch.object(button, "dashboard_is_stopped", return_value=True):
            button.wait_for_capture(time.monotonic() + 5)

    def test_dashboard_loss_during_save_cancels_poweroff(self):
        refused = URLError(ConnectionRefusedError())
        with patch.object(button, "recording_request", side_effect=[status(True, "finalizing", "finalizing"), refused]), patch.object(button, "dashboard_is_stopped") as stopped:
            with self.assertRaisesRegex(RuntimeError, "status unavailable"):
                button.wait_for_capture(time.monotonic() + 5)
        stopped.assert_not_called()

    def test_conflict_stop_does_not_power_off_during_other_stop(self):
        conflict = HTTPError("", 409, "", None, None)
        self.check_capture([status(True, take="recording"), conflict, status(True, "finalizing", "finalizing"),
                            status(False, "done")], ["status", "stop", "status", "status"])

    def test_short_press_and_exactly_one_long_press(self):
        rd, wr = os.pipe2(os.O_NONBLOCK)
        calls = []
        def loop():
            try:
                button.watch_button(rd, lambda: calls.append("poweroff"))
            except OSError:
                pass
        def send(value):
            os.write(wr, button.EVENT.pack(0, 0, button.EV_KEY, button.KEY_POWER, value))
        with patch.object(button, "HOLD_SECONDS", 0.08), patch.object(button, "key_is_down", return_value=True):
            worker = threading.Thread(target=loop)
            worker.start()
            try:
                send(1)
                send(0)
                time.sleep(0.15)
                self.assertEqual(calls, [])
                send(1)
                send(2)
                time.sleep(0.18)
                send(2)
                time.sleep(0.15)
                send(0)
                self.assertEqual(calls, ["poweroff"])
            finally:
                os.close(wr)
                worker.join(2)
                os.close(rd)


if __name__ == "__main__":
    unittest.main()
