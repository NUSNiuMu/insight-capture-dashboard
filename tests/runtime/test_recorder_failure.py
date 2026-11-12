"""A dead file writer must end the Take even with live image audit callbacks."""
import asyncio
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from insight_capture.api.routes.recording import RecordingRoutes
from insight_capture.runtime.recording.manager import RecordingManager
from insight_capture.runtime.take import SessionTakeStore
from insight_capture.runtime.tasks import CaptureTask, CaptureTaskCatalog


def failed_capture(tmp_path, monkeypatch, exit_code, audit=True, mode="capture"):
    from insight_capture.runtime.recording import manager as module
    monkeypatch.setattr(module, "capture_network_snapshot", lambda: None)
    store = SessionTakeStore(tmp_path / "results", task_catalog=CaptureTaskCatalog(
        [CaptureTask("qa", "QA", "Failure test", "capture")], default_task_id="qa"))
    take = store.reserve_take()
    manager = RecordingManager({}, 20, tmp_path / "bags", 1024, ["/camera"],
        stop_image_recording=lambda: {}, preview_control=SimpleNamespace(pause=Mock(), resume=Mock()))
    manager.output_path = str(manager.rosbag_root / take["bag_name"])
    manager._staging_dir = manager.rosbag_root / "_staging" / take["bag_name"]
    manager._staging_dir.mkdir(parents=True)
    raw = manager._staging_dir / "partial.mcap"
    raw.write_bytes(b"unfinished original data")
    manager.started_at = time.time() - 2
    manager.recording_mode = mode
    manager._recording_manifest = {}
    manager._image_writer_active = audit
    code = "import time; time.sleep(10)" if exit_code == -9 else f"raise SystemExit({exit_code})"
    process = subprocess.Popen([sys.executable, "-c", code])
    if exit_code == -9:
        process.kill()
    process.wait(timeout=3)
    manager.processes = {"single": process}
    store.mark_recording(manager.output_path)
    store.complete_current = Mock(wraps=store.complete_current)
    context = SimpleNamespace(recording_manager=manager, take_store=store,
        node=SimpleNamespace(get_logger=lambda: SimpleNamespace(warning=Mock())))
    return manager, store, RecordingRoutes(context), raw


@pytest.mark.parametrize("exit_code", [0, 7, -9])
@pytest.mark.parametrize("audit", [False, True])
def test_unsolicited_writer_exit_is_finalized_once_and_originals_remain(tmp_path, monkeypatch, exit_code, audit):
    manager, store, routes, raw = failed_capture(tmp_path, monkeypatch, exit_code, audit)
    before = (raw.read_bytes(), raw.stat().st_mtime_ns)
    failure = manager.status()["recorder_failure"]
    assert failure["exit_codes"] == {"single": exit_code}
    # The transition is still owned until finalization releases it, even when
    # there was no image audit keeping is_recording() true.
    assert manager.is_recording()
    with pytest.raises(RuntimeError, match="already running"):
        manager.start(bag_name="must-not-start")
    async def scenario():
        await routes._finalize_failed_recorder()
        await routes._finalize_failed_recorder()
    asyncio.run(scenario())
    status = manager.status()
    assert not status["recording"] and status["merge_state"] == "error"
    assert status["recording_timing"]["stopped_at_epoch_s"] == failure["detected_at_epoch_s"]
    assert store.current()["state"] == "finalization_failed"
    assert store.current()["recorder_failure"]["exit_codes"] == {"single": exit_code}
    assert store.complete_current.call_count == 1
    assert (raw.read_bytes(), raw.stat().st_mtime_ns) == before
    assert not Path(manager.output_path).exists()
    manager.preview_control.resume.assert_called_with("recording")


def test_watchdog_runs_without_any_status_client(tmp_path, monkeypatch):
    manager, store, routes, _ = failed_capture(tmp_path, monkeypatch, -9)
    async def scenario():
        await routes.start_watchdog(None)
        try:
            async def finished():
                while store.current()["state"] == "recording":
                    await asyncio.sleep(.01)
            await asyncio.wait_for(finished(), timeout=3)
        finally:
            await routes.stop_watchdog(None)
    asyncio.run(scenario())
    assert store.current()["state"] == "finalization_failed"
    assert not manager.is_recording()


def test_watchdog_respects_an_existing_transition(tmp_path, monkeypatch):
    manager, store, routes, _ = failed_capture(tmp_path, monkeypatch, -9)
    async def scenario():
        async with routes._transition:
            await routes._finalize_failed_recorder()
            assert store.current()["state"] == "recording"
        await routes._finalize_failed_recorder()
    asyncio.run(scenario())
    assert store.complete_current.call_count == 1


def test_late_failure_observer_cannot_stop_a_different_take(tmp_path, monkeypatch):
    manager, store, routes, _ = failed_capture(tmp_path, monkeypatch, -9)
    original_stop = routes.stop_recording
    async def switched_before_stop(**kwargs):
        manager.output_path = str(tmp_path / "new-take")
        return await original_stop(**kwargs)
    routes.stop_recording = switched_before_stop
    with pytest.raises(RuntimeError, match="采集已变化"):
        asyncio.run(routes._finalize_failed_recorder())
    assert manager.is_recording()
    store.complete_current.assert_not_called()


def test_requested_shutdown_exit_is_not_an_unexpected_failure(tmp_path):
    manager = RecordingManager({}, 20, tmp_path, 1024, ["/camera"])
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=3)
    manager.processes = {"single": process}
    manager.merge_state = "finalizing"
    assert manager.status()["recorder_failure"] is None


def test_calibration_writer_failure_does_not_complete_a_capture_take(tmp_path, monkeypatch):
    manager, store, routes, _ = failed_capture(tmp_path, monkeypatch, -9, mode="vio_calibration")
    asyncio.run(routes._finalize_failed_recorder())
    assert manager.status()["merge_state"] == "error"
    store.complete_current.assert_not_called()
