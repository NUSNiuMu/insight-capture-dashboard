import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from insight_capture.core.bag import recording_timing
from insight_capture.runtime.recording.manager import RecordingManager
from insight_capture.runtime.take import SessionTakeStore
from insight_capture.runtime.tasks import CaptureTask, CaptureTaskCatalog


def store_at(root):
    return SessionTakeStore(root, task_catalog=CaptureTaskCatalog(
        [CaptureTask("qa", "QA", "QA timing", "capture")], default_task_id="qa"))


def write_metadata(bag, duration_ns=25_541_367_571, messages=100):
    bag.mkdir(exist_ok=True)
    (bag / "data.mcap").write_bytes(b"fixture")
    (bag / "metadata.yaml").write_text(yaml.safe_dump({"rosbag2_bagfile_information": {
        "storage_identifier": "mcap", "relative_file_paths": ["data.mcap"],
        "starting_time": {"nanoseconds_since_epoch": 110_000_000_000},
        "duration": {"nanoseconds": duration_ns}, "message_count": messages,
    }}))


def test_take_uses_recorder_start_not_reservation_and_saved_data_span(tmp_path):
    store = store_at(tmp_path / "results")
    with patch("insight_capture.runtime.take.time.time", return_value=100.):
        store.reserve_take()
    bag = tmp_path / "bag"
    live = {"source": "recorder", "started_at_epoch_s": 110., "stopped_at_epoch_s": None, "duration_s": None}
    store.mark_recording(bag, {"recording_timing": live})
    assert store.current()["start_epoch_s"] == 100.  # Lifecycle provenance retained.
    assert store.current()["recording_timing"]["started_at_epoch_s"] == 110.
    stopped = {**live, "stopped_at_epoch_s": 135., "duration_s": 25.}
    write_metadata(bag)
    with patch("insight_capture.runtime.take.time.time", return_value=150.):
        store.complete_current({"output_path": str(bag), "merge_state": "done", "recording_timing": stopped})
    result = store.current()
    assert result["end_epoch_s"] == 150.
    assert result["recording_timing"]["source"] == "mcap_metadata"
    assert result["recording_timing"]["duration_s"] == 25.541367571


def test_historical_enrichment_preserves_take_and_bag_bytes(tmp_path):
    store = store_at(tmp_path / "results")
    take = store.reserve_take()
    bag = tmp_path / "bag"
    write_metadata(bag)
    store.mark_recording(bag)
    store.complete_current({"output_path": str(bag), "merge_state": "done"})
    paths = [store._path(take["take_id"]), bag / "metadata.yaml", bag / "data.mcap"]
    before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths]
    assert store.task_recordings("qa")["takes"][0]["recording_timing"]["duration_s"] == 25.541367571
    assert store.current()["recording_timing"]["source"] == "mcap_metadata"
    assert [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths] == before


def test_failed_start_does_not_invent_recorded_duration(tmp_path):
    store = store_at(tmp_path)
    store.reserve_take()
    store.fail_start("camera disconnected")
    assert store.current().get("recording_timing") is None


@pytest.mark.parametrize("kind", ["missing", "empty", "negative", "invalid_yaml", "list_yaml", "infinite"])
def test_unreadable_or_empty_recording_has_unknown_data_duration(tmp_path, kind):
    bag = tmp_path / "bag"
    write_metadata(bag, duration_ns=-1 if kind == "negative" else 5,
                   messages=0 if kind == "empty" else 1)
    if kind == "missing":
        (bag / "data.mcap").unlink()
    if kind in {"invalid_yaml", "list_yaml", "infinite"}:
        (bag / "metadata.yaml").write_text({
            "invalid_yaml": "[broken", "list_yaml": "- item",
            "infinite": "rosbag2_bagfile_information:\n  storage_identifier: mcap\n  message_count: 1\n  starting_time: {nanoseconds_since_epoch: .inf}\n  duration: {nanoseconds: 1}\n",
        }[kind])
    assert recording_timing(bag) is None


def test_metadata_update_refreshes_duration_without_stale_cache(tmp_path):
    write_metadata(tmp_path, duration_ns=1_000_000_000)
    assert recording_timing(tmp_path)["duration_s"] == 1.
    write_metadata(tmp_path, duration_ns=2_000_000_000)
    assert recording_timing(tmp_path)["duration_s"] == 2.


def test_manager_exposes_recorder_timing_not_finalization_time(tmp_path):
    manager = RecordingManager({}, 20, tmp_path, 1024, [])
    assert manager._status_unlocked()["recording_timing"] is None
    manager.started_at = 110.
    manager._recording_manifest = {"stopped_at_epoch_s": 135., "duration_s": 25.}
    with patch("insight_capture.runtime.recording.manager.time.time", return_value=900.):
        assert manager._status_unlocked()["recording_timing"] == {
            "source": "recorder", "started_at_epoch_s": 110.,
            "stopped_at_epoch_s": 135., "duration_s": 25.,
        }


def test_mobile_save_timer_freezes_while_worker_finalizes(monkeypatch, tmp_path):
    from insight_capture.api.routes import mobile
    node = SimpleNamespace(cameras=[], poses=[], _preview_manager=SimpleNamespace(paused=True))
    context = SimpleNamespace(results_root=tmp_path, node=node, recording_manager=SimpleNamespace(rosbag_root=tmp_path),
        prepared_playback_manager=SimpleNamespace(status=lambda: {"state": "idle"}),
        take_store=SimpleNamespace(task_status=lambda: {"active": True}, current=lambda: None),
        capture_preflight=SimpleNamespace(camera_stale_sec=2))
    route = mobile.MobileCaptureRoutes(context, None)
    route._task = SimpleNamespace(done=lambda: False)
    route._operation = {"action": "stop", "accepted_at_epoch_s": 135.}
    route._last_status = {"recording": True, "recording_timing": {
        "source": "recorder", "started_at_epoch_s": 110., "stopped_at_epoch_s": None, "duration_s": None}}
    monkeypatch.setattr(mobile, "evaluate_calibration_cameras", lambda *a: ({}, []))
    monkeypatch.setattr(mobile, "resolved_cameras", lambda: [])
    for now in [140., 500.]:
        monkeypatch.setattr(mobile.time, "time", lambda: now)
        result = route._snapshot()
        assert result["state"] == "saving"
        assert result["recording_timing"]["duration_s"] == 25.
    assert route._last_status["recording_timing"]["duration_s"] is None
