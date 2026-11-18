import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from insight_capture.api.context import capture_transition_lock
from insight_capture.api.routes import time_sync as module


class Request:
    can_read_body = True
    def __init__(self, **body):
        self.body = {"epoch_ms": 1794900000000, "rtt_ms": 4, **body}
    async def json(self):
        return self.body


class PhoneClockTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.context = SimpleNamespace(node=SimpleNamespace(get_logger=lambda: SimpleNamespace(warning=lambda *_: None)))
        self.route = module.TimeSyncRoutes(self.context)

    async def test_phone_sample_goes_to_fixed_host_helper(self):
        with mock.patch.object(self.route, "_host_sync", return_value={"state": "synced"}) as helper:
            response = await self.route.synchronize(Request())
            self.assertEqual(response.status, 202)
            await self.route._task
            self.assertAlmostEqual(helper.call_args.args[0]["epoch_s"], 1794900000.002)
            self.assertEqual(self.context.phone_time_sync["state"], "synced")
            self.assertFalse(capture_transition_lock(self.context).locked())

    async def test_transition_rejects_without_queueing_stale_time(self):
        async with capture_transition_lock(self.context):
            with self.assertRaises(RuntimeError):
                await self.route.synchronize(Request())

    async def test_helper_failure_can_retry(self):
        with mock.patch.object(self.route, "_host_sync", side_effect=OSError("unavailable")):
            await self.route.synchronize(Request());await self.route._task
            self.assertEqual(self.context.phone_time_sync["state"], "error")
        with mock.patch.object(self.route, "_host_sync", return_value={"state": "synced"}):
            await self.route.synchronize(Request());await self.route._task
            self.assertEqual(self.context.phone_time_sync["state"], "synced")

    async def test_nonfinite_dates_and_long_rtt_rejected(self):
        for body in [{"epoch_ms": 0}, {"epoch_ms": float("nan")}, {"epoch_ms": True}, {"rtt_ms": -1}, {"rtt_ms": 3000}]:
            with self.assertRaises(ValueError):await self.route.synchronize(Request(**body))


from insight_capture.runtime import discovery

class HostClockTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        wall = mock.patch.object(discovery.time, 'time', return_value=1700000000.0)
        wall.start();self.addCleanup(wall.stop)
        mounts = [{"Source": self.directory.name, "Destination": "/workspaces/insight_capture/config", "Type": "bind"}]
        patch = mock.patch.object(discovery.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(mounts)))
        patch.start();self.addCleanup(patch.stop)

    def sample(self, epoch=None):
        return {"epoch_s": epoch if epoch is not None else 1794900000, "received_monotonic_s": time.monotonic()}

    def test_idle_step_and_current_dynamic_camera_links(self):
        peers = [{"host": "169.254.40.1", "namespace": "head"}, {"host": "169.254.50.1", "namespace": "wrist"}]
        with mock.patch.object(discovery, "json_request", return_value={"state": "idle"}), mock.patch.object(discovery, "resolved_cameras", return_value=peers), mock.patch.object(discovery.time, "clock_settime") as setter, mock.patch.object(discovery, "restart_acquisition") as restart:
            result = discovery.synchronize_phone_clock(self.sample(), {})
            setter.assert_called_once();self.assertEqual(restart.call_count, 2)
            self.assertEqual(result["state"], "synced")

    def test_busy_never_changes_clock(self):
        for state in ["starting", "recording", "saving", "playback"]:
            with mock.patch.object(discovery, "json_request", return_value={"state": state}), mock.patch.object(discovery.time, "clock_settime") as setter:
                self.assertEqual(discovery.synchronize_phone_clock(self.sample(), {})["state"], "deferred")
                setter.assert_not_called()

    def test_small_offset_avoids_camera_restart(self):
        with mock.patch.object(discovery, "json_request", return_value={"state": "idle"}), mock.patch.object(discovery.time, "clock_settime") as setter, mock.patch.object(discovery, "resolved_cameras") as peers:
            self.assertEqual(discovery.synchronize_phone_clock(self.sample(time.time()), {})["state"], "synced")
            setter.assert_not_called();peers.assert_not_called()

    def test_camera_recovery_retries_after_host_clock_already_corrected(self):
        recovery = {};peers=[{"host":"169.254.40.1","namespace":"head"}]
        with mock.patch.object(discovery, "json_request", return_value={"state":"idle"}), mock.patch.object(discovery, "resolved_cameras", return_value=peers), mock.patch.object(discovery.time, "clock_settime"), mock.patch.object(discovery, "restart_acquisition", side_effect=OSError("offline")):
            with self.assertRaises(OSError):discovery.synchronize_phone_clock(self.sample(),recovery)
        self.assertTrue(recovery["required"])
        with mock.patch.object(discovery, "json_request", return_value={"state":"idle"}), mock.patch.object(discovery, "resolved_cameras", return_value=peers), mock.patch.object(discovery.time, "clock_settime") as setter, mock.patch.object(discovery, "restart_acquisition") as restart:
            discovery.synchronize_phone_clock(self.sample(time.time()),recovery)
            setter.assert_not_called();restart.assert_called_once();self.assertFalse(recovery["required"])

    def test_expired_sample_rejected(self):
        body=self.sample();body["received_monotonic_s"]-=10
        with self.assertRaises(ValueError):discovery.synchronize_phone_clock(body,{})

    def test_other_process_lock_prevents_clock_change(self):
        import fcntl
        lock=Path(self.directory.name)/'.capture-upgrade.lock'
        with lock.open('w')as stream:
            fcntl.flock(stream,fcntl.LOCK_SH|fcntl.LOCK_NB)
            with mock.patch.object(discovery.time, "clock_settime")as setter:
                with self.assertRaises(BlockingIOError):discovery.synchronize_phone_clock(self.sample(),{})
                setter.assert_not_called()

    def test_source_mount_uses_same_config_lock_as_recording(self):
        config = Path(self.directory.name) / 'config'
        config.mkdir()
        mounts = [{"Source": self.directory.name, "Destination": "/workspaces/insight_capture", "Type": "bind"}]
        with mock.patch.object(discovery.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(mounts))), mock.patch.object(discovery, "json_request", return_value={"state": "idle"}):
            result = discovery.synchronize_phone_clock(self.sample(time.time()), {})
        self.assertEqual(result['state'], 'synced')
        self.assertTrue((config / '.capture-upgrade.lock').is_file())
        self.assertFalse((Path(self.directory.name) / '.capture-upgrade.lock').exists())
