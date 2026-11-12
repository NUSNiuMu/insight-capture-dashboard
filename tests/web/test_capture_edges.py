"""Regression coverage for capture transitions, clocks, and invalid inputs."""

import asyncio
import struct
import threading
import time
from collections import deque

import pytest
from types import SimpleNamespace
from insight_capture.api.routes.recording import RecordingRoutes
from insight_capture.api.routes.tasks import TaskRoutes
from insight_capture.runtime.tasks import CaptureTask, CaptureTaskCatalog
from insight_capture.runtime.take import SessionTakeStore

def test_task_cannot_change_during_preflight(tmp_path):

    async def scenario():
        store = SessionTakeStore(tmp_path, task_catalog=CaptureTaskCatalog([CaptureTask('qa_a', 'A', 'A', 'capture'), CaptureTask('qa_b', 'B', 'B', 'capture')], default_task_id='qa_a'))
        (entered, release) = (threading.Event(), threading.Event())

        def preflight(*a, **kw):
            entered.set()
            release.wait(5)
            return {'ok': True}
        manager = SimpleNamespace(status=lambda: {'recording': False}, start=lambda **kw: {'recording': True, 'output_path': str(tmp_path / kw['bag_name'])})
        context = SimpleNamespace(take_store=store, recording_manager=manager, capture_preflight=SimpleNamespace(evaluate=preflight))
        recording = RecordingRoutes(context)
        tasks = TaskRoutes(context, recording)
        operation = asyncio.create_task(recording._start_with_preflight(topics=None, bag_name=None))
        await asyncio.to_thread(entered.wait, 3)
        try:
            response = await tasks._handle_activate(SimpleNamespace(match_info={'task_id': 'qa_b'}))
            assert response.status == 409, 'Task changed while an accepted recording start was checking inputs'
        finally:
            release.set()
            await operation
        assert store.task_status()['task']['task_id'] == 'qa_a'
    asyncio.run(scenario())
from insight_capture.runtime.preflight import CapturePreflight
from insight_capture.runtime.recording import clock_sync as clocks
from insight_capture.media.image_pipeline import ImagePipeline

@pytest.mark.parametrize('fault,code', [('healthy', None), ('image', 'camera_inputs_missing'), ('vio', 'camera_inputs_missing'), ('ntp', 'clock_not_ready'), ('epoch', 'image_clock_invalid'), ('disk_full', 'storage_low'), ('readonly', 'storage_unwritable'), ('publisher', 'recorder_topics_missing')])
def test_preflight_fault_matrix(tmp_path, monkeypatch, fault, code):
    now = time.monotonic()
    names = ['insight3_a', 'insight3_b', 'insight9_a']
    cameras = [SimpleNamespace(name=n, device_name=n, topic='/' + n + '/camera/image', label=n) for n in names]
    node = SimpleNamespace(cameras=cameras, poses=[SimpleNamespace(name=n) for n in names], camera_input_lock=threading.Lock(), camera_input_times={n: deque([now - 0.03, now]) for n in names}, camera_liveness_times={n: now for n in names}, _image_pipeline=SimpleNamespace(clock_status=lambda: {'ok': fault != 'epoch'}))
    if fault == 'image':
        node.camera_input_times[names[0]].clear()
    if fault == 'vio':
        node.camera_liveness_times[names[0]] = 0
    topics = [c.topic for c in cameras]
    manager = SimpleNamespace(rosbag_root=tmp_path, default_topics=topics, storage_status={}, clock_monitor=SimpleNamespace(status=lambda: {'ok': fault != 'ntp'}), current_topic_catalog=lambda **kw: {'topics': topics[:-1] if fault == 'publisher' else topics})
    import insight_capture.runtime.preflight as pre
    monkeypatch.setattr(pre.shutil, 'disk_usage', lambda p: SimpleNamespace(free=0 if fault == 'disk_full' else 10 * 1024 ** 3))
    monkeypatch.setattr(pre, 'probe_recording_root', lambda p: 'readonly' if fault == 'readonly' else None)
    r = CapturePreflight(node, manager).evaluate(require_global_pose=False)
    assert r['ok'] == (code is None)
    if code:
        assert code in {f['code'] for f in r['failures']}

@pytest.mark.parametrize('difference,age,good', [(0, 0, True), (-2, 0, True), (1, 0, True), (-2.001, 0, False), (1.001, 0, False), (0, 2.1, False)])
def test_image_epoch_boundaries(difference, age, good):
    n = SimpleNamespace(camera_input_lock=threading.Lock(), cameras=[SimpleNamespace(name='cam')])
    p = ImagePipeline(n)
    p._clock_samples['cam'] = (int((100 + difference) * 1000000000.0), int(100 * 1000000000.0), time.monotonic() - age)
    assert p.clock_status()['ok'] == good

@pytest.mark.parametrize('fault', ['valid', 'short', 'unsynced', 'client_mode', 'bad_origin', 'bad_stratum', 'host_clock_step'])
def test_ntp_reply_validation(monkeypatch, fault):

    class FakeSocket:

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def settimeout(self, *a):
            pass

        def connect(self, *a):
            pass

        def setsockopt(self, *a):
            pass

        def send(self, packet):
            self.packet = packet

        def recvmsg(self, *a):
            reply = bytearray(48)
            reply[0] = 0x24
            reply[1] = 11
            reply[24:32] = self.packet[40:48]
            for (index, stamp) in [(32, 1000.001), (40, 1000.002)]:
                sec = stamp + clocks.NTP_EPOCH
                struct.pack_into('!II', reply, index, int(sec), int(sec % 1 * 2 ** 32))
            if fault == 'short':
                reply = reply[:10]
            if fault == 'unsynced':
                reply[0] |= 0xC0
            if fault == 'client_mode':
                reply[0] = 0x23
            if fault == 'bad_origin':
                reply[24:32] = b'\x00' * 8
            if fault == 'bad_stratum':
                reply[1] = 16
            return (reply, [], 0, None)
    wall = iter([1000.0, 1000.02 if fault == 'host_clock_step' else 1000.004])
    mono = iter([0.0, 0.004])
    monkeypatch.setattr(clocks.socket, 'socket', lambda *a: FakeSocket())
    monkeypatch.setattr(clocks.time, 'time', lambda: next(wall))
    monkeypatch.setattr(clocks.time, 'monotonic', lambda: next(mono))
    if fault == 'valid':
        assert clocks.ntp_sample('192.0.2.1')['offset_bound_ms'] < 2.1
    else:
        with pytest.raises(ValueError):
            clocks.ntp_sample('192.0.2.1')

def test_clock_result_expires_and_missing_camera_invalidates(monkeypatch):
    monitor = object.__new__(clocks.ClockMonitor)
    monitor._lock = threading.Lock()
    monitor.config = {'discover_usb': True}
    monitor._report = {'ok': True, 'checked_at_epoch_s': time.time() - 31, 'devices': {'wrist': {'host': '169.254.40.1', 'ok': True}}}
    monkeypatch.setattr(clocks, 'resolved_cameras', lambda: [{'namespace': 'wrist', 'host': '169.254.40.1'}])
    assert not monitor.status()['ok']
    monitor._report['checked_at_epoch_s'] = time.time()
    assert monitor.status()['ok']
    monkeypatch.setattr(clocks, 'resolved_cameras', lambda: [])
    assert not monitor.status()['ok']

@pytest.mark.parametrize('value', [None, False, 7, {}, [], '', '   ', 'x' * 101])
def test_invalid_command_id_never_starts_capture(value):
    from insight_capture.api.routes.mobile import MobileCaptureRoutes

    class Request:
        headers = {}
        can_read_body = True
        match_info = {'action': 'start'}

        async def json(self):
            return {'command_id': value}
    route = MobileCaptureRoutes(SimpleNamespace(), SimpleNamespace())
    with pytest.raises(ValueError):
        asyncio.run(route.command(Request()))

def test_recording_status_uses_current_clock_not_start_snapshot(monkeypatch):
    from insight_capture.api.routes import mobile
    current = {'ok': False, 'checked_at_epoch_s': 12345, 'age_sec': 1}
    node = SimpleNamespace(cameras=[], poses=[], _preview_manager=SimpleNamespace(paused=True), _image_pipeline=SimpleNamespace(clock_status=lambda: {'ok': False}))
    manager = SimpleNamespace(status=lambda: {'recording': True}, clock_monitor=SimpleNamespace(config={}, status=lambda: dict(current)))
    context = SimpleNamespace(node=node, recording_manager=manager, prepared_playback_manager=SimpleNamespace(status=lambda: {'state': 'idle'}), take_store=SimpleNamespace(task_status=lambda: {'active': True}, current=lambda: None), capture_preflight=SimpleNamespace(camera_stale_sec=2))
    monkeypatch.setattr(mobile, 'evaluate_calibration_cameras', lambda *a: ({}, []))
    monkeypatch.setattr(mobile, 'resolved_cameras', lambda: [])
    route = mobile.MobileCaptureRoutes(context, None)
    route._last_preflight = {'clocks': {'ok': True, 'checked_at_epoch_s': 1}, 'image_clocks': {'ok': True}}
    value = route._snapshot()
    assert value['preflight']['clocks'] == current
    assert value['preflight']['image_clocks']['ok'] is False
    assert value['recording'] is True
