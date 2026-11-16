"""Take decisions must retain the identity the operator actually reviewed."""
import asyncio
import json
from types import SimpleNamespace
import pytest
from insight_capture.api.routes.takes import TakeRoutes
from insight_capture.runtime.take import SessionTakeStore
from insight_capture.runtime.tasks import CaptureTask, CaptureTaskCatalog

class Request:
    can_read_body = True
    def __init__(self, body): self.body = body
    async def json(self): return self.body

def _fixture(root):
    catalog = CaptureTaskCatalog([CaptureTask("task_a", "A", "A", "capture"),
        CaptureTask("task_b", "B", "B", "capture")], default_task_id="task_a")
    store = SessionTakeStore(root / "results", task_catalog=catalog)
    raw = root / "raw.mcap"
    raw.write_bytes(b"original dummy raw data")
    def complete():
        take = store.reserve_take()
        store.mark_recording(str(raw))
        return store.complete_current({"output_path": str(raw)})
    first = complete()
    routes = TakeRoutes(SimpleNamespace(take_store=store))
    return store, routes, raw, first, complete

def _request(routes, body):
    return asyncio.run(routes._handle_reject_current(Request(body)))

@pytest.mark.parametrize("change", ["new_take", "other_task_same_take_id", "task_ended"])
def test_stale_review_never_rejects_a_new_current_take(tmp_path, change):
    store, routes, raw, first, complete = _fixture(tmp_path)
    if change == "new_take": complete()
    elif change == "other_task_same_take_id":
        store.activate_task("task_b")
        complete()
    else: store.end_task()
    before, original = store.current(), raw.read_bytes()
    response = _request(routes, {"task_id": "task_a", "take_id": first["take_id"], "reason": "遮挡"})
    assert response.status == 409, {"status": response.status, "decision": (store.current() or {}).get("operator_decision"), "current_task": store.task_id, "current_take": (store.current() or {}).get("take_id")}
    assert store.current() == before
    assert raw.read_bytes() == original

def test_matching_review_keeps_reason_and_raw_data(tmp_path):
    store, routes, raw, first, complete = _fixture(tmp_path)
    response = _request(routes, {"task_id": "task_a", "take_id": first["take_id"], "reason": "遮挡"})
    assert response.status == 200
    take = json.loads(response.text)["take"]
    assert take["operator_valid"] is False and take["reject_reason"] == "遮挡"
    assert raw.read_bytes() == b"original dummy raw data"

def test_unqualified_script_review_keeps_current_take_semantics(tmp_path):
    store, routes, raw, first, complete = _fixture(tmp_path)
    response = _request(routes, {"reason": "script_review"})
    assert response.status == 200
    assert store.current()["reject_reason"] == "script_review"

@pytest.mark.parametrize("body", [
    {"task_id": "task_a"}, {"take_id": 1}, {"task_id": None, "take_id": 1},
    {"task_id": " ", "take_id": 1}, {"task_id": "task_a", "take_id": None},
    {"task_id": "task_a", "take_id": True}, {"task_id": "task_a", "take_id": "1"},
    {"task_id": "task_a", "take_id": 0}, {"task_id": "task_a", "take_id": -1},
])
def test_incomplete_review_identity_cannot_change_a_decision(tmp_path, body):
    store, routes, raw, first, complete = _fixture(tmp_path)
    before = store.current()
    assert _request(routes, body).status == 400
    assert store.current() == before


def test_repeat_confirmation_preserves_decision_time_but_new_reason_updates_it(tmp_path, monkeypatch):
    import insight_capture.runtime.take as module
    store, routes, raw, first, complete = _fixture(tmp_path)
    times = iter([123.0, 456.0])
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: next(times)))
    body = {"task_id": "task_a", "take_id": first["take_id"], "reason": "遮挡"}
    assert _request(routes, body).status == 200
    decided = store.current()
    assert _request(routes, body).status == 200
    assert store.current() == decided
    assert decided["operator_decided_at_epoch_s"] == 123.0
    assert _request(routes, {**body, "reason": "画面模糊"}).status == 200
    assert store.current()["operator_decided_at_epoch_s"] == 456.0
    assert store.current()["reject_reason"] == "画面模糊"


def test_store_rechecks_identity_at_the_actual_decision_write(tmp_path, monkeypatch):
    store, routes, raw, first, complete = _fixture(tmp_path)
    store.activate_task("task_b")
    other = complete()
    store.activate_task("task_a")
    reject = store.reject_current
    def changed_before_write(reason, **identity):
        store.activate_task("task_b")
        return reject(reason, **identity)
    monkeypatch.setattr(store, "reject_current", changed_before_write)
    response = _request(routes, {"task_id": "task_a", "take_id": first["take_id"]})
    assert response.status == 409
    assert store.current() == other
