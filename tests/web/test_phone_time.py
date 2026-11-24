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

    async def test_failed_phone_sync_cannot_restart_cameras_automatically(self):
        with mock.patch.object(self.route, "_host_sync", side_effect=OSError("unavailable")):
            await self.route.synchronize(Request());await self.route._task
            self.assertEqual(self.context.phone_time_sync["state"], "error")
        with mock.patch.object(self.route, "_host_sync") as helper:
            refused = await self.route.synchronize(Request())
            accepted = await self.route.synchronize(Request(epoch_ms=time.time() * 1000))
            self.assertEqual(refused.status, 409)
            self.assertEqual(accepted.status, 200)
            self.assertEqual(self.context.phone_time_sync["state"], "synced")
            helper.assert_not_called()

    def camera_manager(self, measure):
        self.context.recording_manager = SimpleNamespace(
            status=lambda: {"recording": False}, measure_camera_clocks=measure,
        )
        self.context.prepared_playback_manager = SimpleNamespace(status=lambda: {"state": "idle"})

    async def test_aligned_cameras_confirm_without_using_phone_or_host_helper(self):
        clock = {"ok": True, "pairwise_clock_bound_ms": 2.2}
        self.camera_manager(lambda: clock)
        with mock.patch.object(self.route, "_host_sync") as helper:
            response = await self.route.synchronize(Request(epoch_ms=None, resync_cameras=True))
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.text)["clock"], clock)
            self.assertEqual(self.context.camera_time_sync["state"], "synced")
            self.assertEqual(self.context.phone_time_sync["state"], "idle")
            helper.assert_not_called()

    async def test_camera_recovery_is_explicit_once_and_verifies_the_result(self):
        stale = {"ok": False}
        measure = mock.Mock(return_value=stale)
        self.camera_manager(measure)
        with mock.patch.object(self.route, "_host_sync", return_value={"state": "synced"}) as helper:
            check = await self.route.synchronize(Request(resync_cameras=True))
            self.assertEqual(check.status, 200)
            self.assertEqual(json.loads(check.text)["state"], "needs_recovery")
            helper.assert_not_called()
            response = await self.route.synchronize(Request(resync_cameras=True, recover_cameras=True))
            self.assertEqual(response.status, 202)
            await self.route._task
            self.assertEqual(self.context.camera_time_sync["state"], "error")
            self.assertEqual(self.context.phone_time_sync["state"], "idle")
            helper.assert_called_once()
            args = helper.call_args.args[0]
            self.assertTrue(args["resync_cameras"])
            self.assertAlmostEqual(args["epoch_s"], time.time(), delta=1)
            self.assertEqual(measure.call_count, 3)

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
            result = discovery.synchronize_phone_clock(self.sample())
            setter.assert_called_once();self.assertEqual(restart.call_count, 2)
            self.assertEqual(result["state"], "synced")

    def test_busy_never_changes_clock(self):
        for state in ["starting", "recording", "saving", "playback"]:
            with mock.patch.object(discovery, "json_request", return_value={"state": state}), mock.patch.object(discovery.time, "clock_settime") as setter:
                self.assertEqual(discovery.synchronize_phone_clock(self.sample())["state"], "deferred")
                setter.assert_not_called()

    def test_small_offset_avoids_camera_restart(self):
        with mock.patch.object(discovery, "json_request", return_value={"state": "idle"}), mock.patch.object(discovery.time, "clock_settime") as setter, mock.patch.object(discovery, "resolved_cameras") as peers:
            self.assertEqual(discovery.synchronize_phone_clock(self.sample(time.time()))["state"], "synced")
            setter.assert_not_called();peers.assert_not_called()

    def test_failed_camera_recovery_does_not_restart_if_host_is_aligned(self):
        peers = [{"host": "169.254.40.1", "namespace": "head"}]
        with mock.patch.object(discovery, "json_request", return_value={"state": "idle"}), mock.patch.object(discovery, "resolved_cameras", return_value=peers), mock.patch.object(discovery.time, "clock_settime"), mock.patch.object(discovery, "restart_acquisition", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                discovery.synchronize_phone_clock(self.sample())
        with mock.patch.object(discovery, "json_request", return_value={"state": "idle"}), mock.patch.object(discovery.time, "clock_settime") as setter, mock.patch.object(discovery, "restart_acquisition") as restart:
            discovery.synchronize_phone_clock(self.sample(time.time()))
            setter.assert_not_called()
            restart.assert_not_called()

    def test_expired_sample_rejected(self):
        body=self.sample();body["received_monotonic_s"]-=10
        with self.assertRaises(ValueError):discovery.synchronize_phone_clock(body)

    def test_other_process_lock_prevents_clock_change(self):
        import fcntl
        lock=Path(self.directory.name)/'.capture-upgrade.lock'
        with lock.open('w')as stream:
            fcntl.flock(stream,fcntl.LOCK_SH|fcntl.LOCK_NB)
            with mock.patch.object(discovery.time, "clock_settime")as setter:
                with self.assertRaises(BlockingIOError):discovery.synchronize_phone_clock(self.sample())
                setter.assert_not_called()

    def test_source_mount_uses_same_config_lock_as_recording(self):
        config = Path(self.directory.name) / 'config'
        config.mkdir()
        mounts = [{"Source": self.directory.name, "Destination": "/workspaces/insight_capture", "Type": "bind"}]
        with mock.patch.object(discovery.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(mounts))), mock.patch.object(discovery, "json_request", return_value={"state": "idle"}):
            result = discovery.synchronize_phone_clock(self.sample(time.time()))
        self.assertEqual(result['state'], 'synced')
        self.assertTrue((config / '.capture-upgrade.lock').is_file())
        self.assertFalse((Path(self.directory.name) / '.capture-upgrade.lock').exists())

    def test_camera_recovery_never_steps_jetson_clock(self):
        peers=[{"host":"169.254.40.1","namespace":"head"}]
        sample={**self.sample(),'resync_cameras':True}
        with mock.patch.object(discovery, "json_request", return_value={"state":"idle"}), mock.patch.object(discovery, "resolved_cameras", return_value=peers), mock.patch.object(discovery.time, "clock_settime") as setter, mock.patch.object(discovery, "restart_acquisition") as restart:
            self.assertEqual(discovery.synchronize_phone_clock(sample)['state'],'synced')
            setter.assert_not_called();restart.assert_called_once_with(peers[0])

    def test_boot_and_manual_camera_recovery_do_not_interleave(self):
        from concurrent.futures import ThreadPoolExecutor
        calls=[]
        def request(url,*args,**kwargs):
            calls.append(url.rsplit('/',1)[-1]);time.sleep(.01);return {'success':True}
        camera={'host':'169.254.40.1','namespace':'qa_serialization'}
        with mock.patch.object(discovery,'json_request',side_effect=request),ThreadPoolExecutor(max_workers=2)as pool:
            list(pool.map(discovery.restart_acquisition,[camera,camera]))
        self.assertEqual(calls,['insight-stop','insight-start','insight-stop','insight-start'])
