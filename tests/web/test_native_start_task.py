"""Exercise the legacy native HTTP start route with real task allocation."""
import asyncio
import json
from types import SimpleNamespace
import pytest
from insight_capture.api.routes.recording import RecordingRoutes
from insight_capture.runtime.tasks import CaptureTask, CaptureTaskCatalog
from insight_capture.runtime.take import SessionTakeStore

class Request:
    can_read_body = True
    remote = "198.51.100.2"
    headers = {}
    def __init__(self, body):
        self.body = body
    async def json(self):
        return self.body

def _capture_fixture(root, active):
    store = SessionTakeStore(root, task_catalog=CaptureTaskCatalog([
        CaptureTask("task_a", "A", "A", "capture"),
        CaptureTask("task_b", "B", "B", "capture"),
    ], default_task_id=active))
    calls, checks = [], []
    def start(**kwargs):
        calls.append(kwargs)
        return {"recording": True, "output_path": str(root / kwargs["output_subdirectory"] / kwargs["bag_name"])}
    def evaluate(*args, **kwargs):
        checks.append((args, kwargs))
        return {"ok": True}
    preview_events = []
    gate = SimpleNamespace(events=preview_events,
        pause=lambda token: preview_events.append("pause"),
        resume=lambda token: preview_events.append("resume"))
    routes = RecordingRoutes(SimpleNamespace(take_store=store,
        recording_manager=SimpleNamespace(status=lambda: {"recording": False}, start=start, preview_control=gate),
        capture_preflight=SimpleNamespace(evaluate=evaluate)))
    return store, routes, calls, checks

@pytest.mark.parametrize("active", ["task_b", None])
def test_stale_native_start_cannot_allocate_in_another_task(tmp_path, active):
    store, routes, calls, checks = _capture_fixture(tmp_path, active or "task_a")
    if active is None:
        store.end_task()
    response = asyncio.run(routes._handle_recording_start(Request({"task_id": "task_a", "native_relaxed_preflight": True})))
    assert response.status == 409, {"http_status": response.status,
        "recorder_called": bool(calls), "allocated_task": (store.current() or {}).get("task_id")}
    assert json.loads(response.text)["error"] == "capture_task_changed"
    assert calls == checks == []
    assert routes.context.recording_manager.preview_control.events == []
    assert store.current() is None
    assert store.task_status()["active"] is (active is not None)
    if active is not None:
        assert store.task_status()["task"]["task_id"] == active
    assert store.task_status()["stats"]["next_take_id"] == 1

@pytest.mark.parametrize("relaxed", [False, True])
def test_matching_native_task_preserves_recording_options(tmp_path, relaxed):
    store, routes, calls, checks = _capture_fixture(tmp_path, "task_a")
    response = asyncio.run(routes._handle_recording_start(Request({"task_id": "task_a", "native_relaxed_preflight": relaxed, "topics": ["/camera"], "bag_name": "named_take"})))
    assert response.status == 200
    assert store.current()["task_id"] == "task_a"
    assert calls == [{"topics": ["/camera"], "bag_name": "named_take", "require_global_pose": not relaxed, "output_subdirectory": "task_a"}]
    assert checks[0][1]["require_global_pose"] is not relaxed
    assert routes.context.recording_manager.preview_control.events == ["pause", "resume"]

def test_legacy_script_without_task_id_keeps_current_task_semantics(tmp_path):
    store, routes, calls, checks = _capture_fixture(tmp_path, "task_b")
    response = asyncio.run(routes._handle_recording_start(Request({})))
    assert response.status == 200
    assert store.current()["task_id"] == "task_b"
    assert len(calls) == len(checks) == 1

@pytest.mark.parametrize("task_id", [None, "", "  ", 7, False, [], {}])
def test_explicit_invalid_task_id_never_reserves_or_starts(tmp_path, task_id):
    store, routes, calls, checks = _capture_fixture(tmp_path, "task_b")
    response = asyncio.run(routes._handle_recording_start(Request({"task_id": task_id})))
    assert response.status == 400
    assert calls == checks == []
    assert routes.context.recording_manager.preview_control.events == []
    assert store.current() is None
    assert store.task_status()["stats"]["next_take_id"] == 1
