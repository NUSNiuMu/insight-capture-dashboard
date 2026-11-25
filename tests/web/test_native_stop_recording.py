"""Real stop handlers/managers with dummy local and remote writers only."""
import asyncio
import threading
from types import SimpleNamespace
import pytest
from insight_capture.api.middleware import create_json_error_middleware
from insight_capture.api.routes.recording import RecordingRoutes
from insight_capture.runtime.recording.manager import RecordingManager
from insight_capture.runtime.recording.distributed import DistributedRecordingManager
from insight_capture.runtime.take import SessionTakeStore
from insight_capture.runtime.tasks import CaptureTask, CaptureTaskCatalog

class Request:
    path = "/api/recording/stop"
    def __init__(self, body):
        self.body = body
        self.can_read_body = body is not None
    async def json(self):
        return self.body

def _capture_fixture(root, monkeypatch, kind="local", mode="capture"):
    from insight_capture.runtime.recording import manager as module
    monkeypatch.setattr(module, "capture_network_snapshot", lambda: None)
    store = SessionTakeStore(root / "results", task_catalog=CaptureTaskCatalog([
        CaptureTask("task", "Task", "Stop identity", "capture")], default_task_id="task"))
    old = store.reserve_take("recording_a")
    old_path = str(root / "bags" / old["bag_name"])
    store.mark_recording(old_path)
    store.complete_current({"output_path": old_path})
    take = store.reserve_take("recording_b")
    output_path = str(root / "bags" / take["bag_name"])
    store.mark_recording(output_path)
    stopped, cancelled = [], []
    local = RecordingManager({}, 20, root / "bags", 1024, ["/camera"],
        stop_image_recording=lambda: stopped.append(output_path))
    local.output_path, local.recording_mode = output_path, mode
    local._image_writer_active = True
    def publish(*args):
        local.merge_state = "done"
    monkeypatch.setattr(local, "_publish_single_mcap_session", publish)
    manager = local
    if kind in {"distributed", "bypass"}:
        # Preserve the real distributed stop/status/lock path; replace only I/O.
        manager = DistributedRecordingManager.__new__(DistributedRecordingManager)
        manager.local, manager.config = local, {"max_clock_bound_ms": 10}
        manager._lock, manager._lifecycle = threading.RLock(), threading.Lock()
        manager._bypass, manager._baseline = kind == "bypass", None
        manager._clock, manager._last_clock_mono, manager._last_error = {}, 0, None
        manager._worker, manager._callbacks, manager._finished_callbacks = None, [], []
        manager._session = {"output_path": output_path, "phase": "recording", "session_id": "dummy"}
        manager.ego = SimpleNamespace(timeout=.1, cancel=lambda session: cancelled.append(session["output_path"]))
        manager.measure_camera_clocks = lambda: {"ok": True}
        manager._save = lambda: None
    context = SimpleNamespace(recording_manager=manager, take_store=store,
        node=SimpleNamespace(get_logger=lambda: SimpleNamespace(error=lambda message: None)))
    return RecordingRoutes(context), store, local, stopped, cancelled, old_path, output_path

def _request(routes, body):
    return asyncio.run(create_json_error_middleware(routes.context)(Request(body), routes._handle_recording_stop))

@pytest.mark.parametrize("kind", ["local", "distributed", "bypass"])
def test_stale_path_cannot_stop_or_complete_new_take(tmp_path, monkeypatch, kind):
    routes, store, local, stopped, cancelled, old, current = _capture_fixture(tmp_path, monkeypatch, kind,
        "vio_calibration" if kind == "bypass" else "capture")
    before = store.current()
    response = _request(routes, {"output_path": old, "recording_mode": local.recording_mode})
    assert response.status == 409, {"http_status": response.status,
        "stopped_writer": stopped, "cancelled_remote": cancelled, "current": current}
    assert stopped == cancelled == []
    assert local.is_recording()
    assert store.current() == before

@pytest.mark.parametrize("kind,active,requested", [
    ("local", "capture", "vio_calibration"),
    ("local", "vio_calibration", "capture"),
    ("distributed", "capture", "vio_calibration"),
    ("bypass", "vio_calibration", "capture"),
])
def test_same_path_different_mode_cannot_stop(tmp_path, monkeypatch, kind, active, requested):
    routes, store, local, stopped, cancelled, old, current = _capture_fixture(tmp_path, monkeypatch, kind, active)
    before = store.current()
    response = _request(routes, {"output_path": current, "recording_mode": requested})
    assert response.status == 409
    assert stopped == cancelled == [] and local.is_recording()
    assert store.current() == before

@pytest.mark.parametrize("kind,mode", [("local", "capture"), ("local", "vio_calibration"), ("distributed", "capture"), ("bypass", "vio_calibration")])
def test_matching_identity_stops_the_requested_writer(tmp_path, monkeypatch, kind, mode):
    routes, store, local, stopped, cancelled, old, current = _capture_fixture(tmp_path, monkeypatch, kind, mode)
    response = _request(routes, {"output_path": current, "recording_mode": mode})
    assert response.status == 200
    assert stopped == [current] and not local.is_recording()
    assert cancelled == ([current] if kind == "distributed" else [])
    assert (store.current()["state"] == "recording") is (mode == "vio_calibration")

@pytest.mark.parametrize("body", [{}, None])
def test_unqualified_legacy_script_stop_is_preserved(tmp_path, monkeypatch, body):
    routes, store, local, stopped, cancelled, old, current = _capture_fixture(tmp_path, monkeypatch)
    assert _request(routes, body).status == 200
    assert stopped == [current] and not local.is_recording()

@pytest.mark.parametrize("body", [
    {"output_path": None, "recording_mode": "capture"},
    {"output_path": "", "recording_mode": "capture"},
    {"output_path": 3, "recording_mode": "capture"},
    {"output_path": "/dummy"},
    {"recording_mode": "capture"},
    {"output_path": "/dummy", "recording_mode": None},
    {"output_path": "/dummy", "recording_mode": " "},
    {"output_path": "/dummy", "recording_mode": []},
])
def test_explicit_incomplete_identity_never_stops(tmp_path, monkeypatch, body):
    routes, store, local, stopped, cancelled, old, current = _capture_fixture(tmp_path, monkeypatch)
    before = store.current()
    assert _request(routes, body).status == 400
    assert stopped == cancelled == [] and local.is_recording()
    assert store.current() == before


def test_retry_after_lost_stop_response_keeps_completed_take(tmp_path, monkeypatch):
    routes, store, local, stopped, cancelled, old, current = _capture_fixture(tmp_path, monkeypatch)
    body = {"output_path": current, "recording_mode": "capture"}
    assert _request(routes, body).status == 200
    completed = store.current()
    assert _request(routes, body).status == 200
    assert stopped == [current] and cancelled == []
    assert store.current() == completed
