"""Operator warnings must follow the current Take without changing capture."""

from types import SimpleNamespace

import pytest

from insight_capture.api.routes import mobile
from insight_capture.runtime.anomaly import ActiveQcMonitor
from insight_capture.runtime.take import SessionTakeStore
from insight_capture.runtime.tasks import CaptureTask, CaptureTaskCatalog


@pytest.fixture
def capture(tmp_path, monkeypatch):
    store = SessionTakeStore(tmp_path, task_catalog=CaptureTaskCatalog(
        [CaptureTask("qa", "QA", "Live alert test", "capture")], default_task_id="qa"))
    store.reserve_take()
    bag = tmp_path / "bag"
    store.mark_recording(bag)
    state = {"recording": True, "recording_mode": "capture", "output_path": str(bag)}
    node = SimpleNamespace(cameras=[], poses=[], _preview_manager=SimpleNamespace(paused=True),
        build_mapping_payload=lambda: {"statuses": {
            name: {"online": True, "localized": True}
            for name in ("insight9", "insight3_a", "insight3_b")}})
    manager = SimpleNamespace(rosbag_root=tmp_path, status=lambda: dict(state),
                              is_recording=lambda: state["recording"])
    context = SimpleNamespace(results_root=tmp_path, node=node, recording_manager=manager,
        prepared_playback_manager=SimpleNamespace(status=lambda: {"state": "idle"}),
        take_store=store, capture_preflight=SimpleNamespace(camera_stale_sec=2,
            evaluate=lambda **_: {"ok": True, "failures": [], "warnings": []}))
    monkeypatch.setattr(mobile, "evaluate_calibration_cameras", lambda *a: ({}, []))
    monkeypatch.setattr(mobile, "resolved_cameras", lambda: [])
    return SimpleNamespace(store=store, state=state, route=mobile.MobileCaptureRoutes(context, None),
        monitor=ActiveQcMonitor(node, manager, store), manager=manager)


def test_sustained_low_disk_reaches_phone_and_resolution_keeps_history(capture, monkeypatch):
    free = 1024**3
    now = 100.0
    monkeypatch.setattr("insight_capture.runtime.anomaly.shutil.disk_usage",
                        lambda _: SimpleNamespace(free=free, total=20 * 1024**3, used=20 * 1024**3 - free))
    monkeypatch.setattr("insight_capture.runtime.anomaly.time.monotonic", lambda: now)
    capture.monitor.poll()
    assert capture.route._snapshot().get("live_alerts", []) == []  # Existing sustain window.
    now += 4
    capture.monitor.poll()
    assert capture.store.current()["anomaly_timeline"][0]["code"] == "storage_low"
    alert = capture.route._snapshot()["live_alerts"][0]
    assert alert["code"] == "storage_low"
    assert alert["severity"] == "critical"
    assert alert["message"] == "录制存储空间不足，请尽快停止采集。"
    assert capture.manager.is_recording()  # Warning does not stop raw capture.
    free = 10 * 1024**3
    capture.monitor.poll()
    assert capture.route._snapshot()["live_alerts"] == []
    assert [e["event"] for e in capture.store.current()["anomaly_timeline"]] == ["started", "resolved"]


def test_ntp_failure_during_capture_is_visible_and_retained_after_recovery(capture, monkeypatch):
    now = 100.0
    clock = {"ok": True, "checked_at_epoch_s": 123.0, "devices": {}}
    capture.manager.clock_monitor = SimpleNamespace(config={}, status=lambda: dict(clock))
    monkeypatch.setattr("insight_capture.runtime.anomaly.time.monotonic", lambda: now)
    capture.monitor.poll()
    clock.update(ok=False, devices={"head": {"ok": False, "error": "timed out"}})
    capture.monitor.poll()
    now += 1
    clock["ok"] = True
    capture.monitor.poll()
    assert capture.store.current()["anomaly_timeline"] == []  # Brief loss is not sustained.
    clock["ok"] = False
    capture.monitor.poll()
    now += 4
    capture.monitor.poll()  # No browser polling is needed to preserve the fault.
    history = capture.store.current()["anomaly_timeline"]
    assert len(history) == 1 and history[0]["code"] == "clock_sync"
    assert history[0]["details"]["clocks"]["devices"]["head"]["error"] == "timed out"
    alert = capture.route._snapshot()["live_alerts"][0]
    assert alert["code"] == "clock_sync" and alert["severity"] == "warning"
    assert capture.manager.is_recording()  # Never discard raw data on a failed measurement.
    clock.update(ok=True, devices={"head": {"ok": True, "offset_bound_ms": 1.0}})
    capture.monitor.poll()
    assert capture.route._snapshot()["live_alerts"] == []
    take = capture.store.complete_current({"output_path": capture.state["output_path"], "merge_state": "done"})
    assert [e["event"] for e in take["anomaly_timeline"]] == ["started", "resolved"]
    assert take["quick_qc"]["state"] == "suspect"
    # The start sample must survive recovery, not alias the current clock report.
    assert take["anomaly_timeline"][0]["details"]["clocks"]["ok"] is False


def test_next_take_keeps_persistent_warning_when_qc_misses_the_idle_interval(capture, monkeypatch):
    now = 100.0
    monkeypatch.setattr("insight_capture.runtime.anomaly.time.monotonic", lambda: now)
    capture.state["storage"] = {"using_fallback": True}
    capture.monitor.poll()
    now += 4
    capture.monitor.poll()
    first = capture.store.complete_current({"output_path": capture.state["output_path"], "merge_state": "done"})
    assert [e["code"] for e in first["anomaly_timeline"]] == ["storage_fallback"]
    capture.store.reserve_take()
    capture.state["output_path"] += "-next"
    capture.store.mark_recording(capture.state["output_path"])
    # A polling monitor can miss the short idle interval between Takes.
    capture.monitor.poll()
    assert capture.store.current()["anomaly_timeline"] == []
    now += 4
    capture.monitor.poll()
    events = capture.store.current()["anomaly_timeline"]
    assert len(events) == 1 and events[0]["code"] == "storage_fallback"
    assert events[0]["event"] == "started"


@pytest.mark.parametrize("phase,expected", [("recording", True), ("saving", True),
    ("starting", False), ("idle", False), ("saved", False), ("playback", False),
    ("different_bag", False), ("missing_bag", False), ("different_take", False)])
def test_live_alerts_belong_only_to_active_recording_or_its_save(capture, phase, expected):
    capture.store.add_anomaly({"event": "started", "code": "storage_fallback",
        "severity": "warning", "message": "当前正在使用备用存储。"})
    if phase in {"starting", "saving"}:
        capture.route._last_status = dict(capture.state)
        capture.route._task = SimpleNamespace(done=lambda: False)
        capture.route._operation = {"action": "start" if phase == "starting" else "stop",
                                    "accepted_at_epoch_s": 100}
    elif phase in {"idle", "saved"}:
        capture.state.update(recording=False, merge_state="done" if phase == "saved" else None)
    elif phase == "playback":
        capture.route.context.prepared_playback_manager.status = lambda: {"state": "playing"}
    elif phase in {"different_bag", "missing_bag"}:
        capture.state["output_path"] = "/another/bag" if phase == "different_bag" else None
    elif phase == "different_take":
        capture.store.complete_current({"output_path": capture.state["output_path"], "merge_state": "done"})
        capture.store.reserve_take()
    result = capture.route._snapshot()
    assert bool(result["live_alerts"]) is expected
    assert "anomaly_timeline" not in result["current_take"]


def test_alert_recurrence_and_independent_resolution(capture):
    def event(code, kind):
        capture.store.add_anomaly({"code": code, "event": kind, "severity": "critical", "message": code})
    event("storage_low", "started")
    event("recorder_io", "started")
    event("storage_low", "resolved")
    assert [e["code"] for e in capture.route._snapshot()["live_alerts"]] == ["recorder_io"]
    event("storage_low", "started")
    event("storage_low", "started")
    assert {e["code"] for e in capture.route._snapshot()["live_alerts"]} == {"storage_low", "recorder_io"}


def test_malformed_history_does_not_break_mobile_status(capture):
    original = capture.store.current()
    original["anomaly_timeline"] = [None, [], "bad", {"code": []},
        {"code": "broken", "event": "started", "message": {}},
        {"code": "storage_low", "event": "started", "message": "Low storage", "severity": "critical"},
        {"code": "storage_low", "event": "unknown"}]
    capture.route.context.take_store.current = lambda: original
    assert [e["code"] for e in capture.route._snapshot()["live_alerts"]] == ["storage_low"]
