import asyncio
import json
import sqlite3
import threading
from types import SimpleNamespace

from aiohttp import web
import pytest

from insight_capture.api.routes.mobile import MobileCaptureRoutes


class Request:
    headers = {}
    can_read_body = True

    def __init__(self, action, command_id, output_path=None):
        self.match_info = {"action": action, "command_id": command_id}
        self.body = {"command_id": command_id, "output_path": output_path}

    async def json(self):
        return self.body


def harness(tmp_path):
    state = {"recording": False, "recording_mode": "capture", "output_path": None}
    starts = []

    async def start(**_):
        starts.append(len(starts) + 1)
        state.update(recording=True, output_path=f"/take/{len(starts)}")
        return web.json_response({**state, "preflight": {"ok": True}})

    async def stop(**_):
        state.update(recording=False, merge_state="done")
        return web.json_response(state)

    context = SimpleNamespace(results_root=tmp_path,
        recording_manager=SimpleNamespace(status=lambda: dict(state)),
        node=SimpleNamespace(get_logger=lambda: SimpleNamespace(error=lambda _: None)))
    recording = SimpleNamespace(_start_with_preflight=start, stop_recording=stop)
    return context, recording, starts


def test_old_start_result_can_be_queried_after_another_client_stops(tmp_path):
    async def scenario():
        context, recording, starts = harness(tmp_path)
        route = MobileCaptureRoutes(context, recording)
        await route.command(Request("start", "lost-start"))
        await route._task
        await route.command(Request("stop", "another-client-stop", "/take/1"))
        await route._task
        result = json.loads((await route.command_status(Request("start", "lost-start"))).body)
        assert result["operation"]["command_id"] == "lost-start"
        assert result["operation"]["state"] == "complete"
        assert route._operation["action"] == "stop"
        assert starts == [1]
    asyncio.run(scenario())


def test_retry_after_backend_restart_does_not_create_another_take(tmp_path):
    async def scenario():
        context, recording, starts = harness(tmp_path)
        first = MobileCaptureRoutes(context, recording)
        await first.command(Request("start", "lost-start"))
        await first._task
        await first.command(Request("stop", "other-stop", "/take/1"))
        await first._task
        restarted = MobileCaptureRoutes(context, recording)
        response = await restarted.command(Request("start", "lost-start"))
        if restarted._task:
            await restarted._task
        assert json.loads(response.body)["state"] == "complete"
        assert starts == [1], "A replayed start must not allocate a second Take after restart"
    asyncio.run(scenario())


def test_interrupted_receipt_is_uncertain_and_never_replayed(tmp_path):
    async def scenario():
        context, recording, starts = harness(tmp_path)
        first = MobileCaptureRoutes(context, recording)
        first._commands.reserve(("start", None), {"command_id": "interrupted", "action": "start", "state": "running", "error": None})
        restarted = MobileCaptureRoutes(context, recording)
        response = await restarted.command(Request("start", "interrupted"))
        receipt = json.loads(response.body)
        assert receipt["state"] == "interrupted"
        assert "无法确认" in receipt["error"]
        assert starts == []
        assert restarted._operation is None  # Historical uncertainty is not current device failure.
    asyncio.run(scenario())


def test_old_command_survives_more_than_one_hundred_new_receipts(tmp_path):
    async def scenario():
        context, recording, starts = harness(tmp_path)
        route = MobileCaptureRoutes(context, recording)
        for index in range(105):
            await route.command(Request("start", f"start-{index}"))
            await route._task
            await route.command(Request("stop", f"stop-{index}", f"/take/{index+1}"))
            await route._task
        await route.command(Request("start", "start-0"))
        await route._task
        assert len(starts) == 105
    asyncio.run(scenario())


def test_changed_request_identity_still_rejected_after_restart(tmp_path):
    async def scenario():
        context, recording, starts = harness(tmp_path)
        route = MobileCaptureRoutes(context, recording)
        await route.command(Request("start", "same-id"))
        await route._task
        restarted = MobileCaptureRoutes(context, recording)
        with pytest.raises(RuntimeError, match="different operation"):
            await restarted.command(Request("stop", "same-id", "/take/1"))
        assert len(starts) == 1
    asyncio.run(scenario())


def test_receipt_storage_failure_does_not_start_recording(tmp_path, monkeypatch):
    async def scenario():
        context, recording, starts = harness(tmp_path)
        route = MobileCaptureRoutes(context, recording)
        def fail(*_):
            raise sqlite3.OperationalError("disk full")
        monkeypatch.setattr(route._commands, "reserve", fail)
        with pytest.raises(sqlite3.OperationalError):
            await route.command(Request("start", "not-admitted"))
        assert starts == []
        assert route._task is None
        assert route._operation is None
    asyncio.run(scenario())


def test_result_write_failure_preserves_completed_action_and_blocks_replay(tmp_path, monkeypatch):
    async def scenario():
        context, recording, starts = harness(tmp_path)
        route = MobileCaptureRoutes(context, recording)
        def fail(*_):
            raise sqlite3.OperationalError("disk full")
        monkeypatch.setattr(route._commands, "finish", fail)
        await route.command(Request("start", "acted"))
        await route._task
        assert route._operation["state"] == "complete"
        assert route._operation["error"] is None
        restarted = MobileCaptureRoutes(context, recording)
        result = json.loads((await restarted.command(Request("start", "acted"))).body)
        assert result["state"] == "interrupted"
        assert starts == [1]
    asyncio.run(scenario())


def test_disconnect_during_receipt_commit_does_not_orphan_accepted_command(tmp_path, monkeypatch):
    async def scenario():
        context, recording, starts = harness(tmp_path)
        route = MobileCaptureRoutes(context, recording)
        entered, release = threading.Event(), threading.Event()
        reserve = route._commands.reserve
        def delayed(*args):
            reserve(*args)
            entered.set()
            release.wait(3)
        monkeypatch.setattr(route._commands, "reserve", delayed)
        request = asyncio.create_task(route.command(Request("start", "disconnected")))
        assert await asyncio.to_thread(entered.wait, 3)
        request.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        await route._task
        await route.command(Request("start", "disconnected"))
        assert starts == [1]
        assert route._commands.get("disconnected")[1]["state"] == "complete"
    asyncio.run(scenario())
