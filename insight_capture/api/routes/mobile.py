"""Phone controls using the shared capture and distributed recording lifecycle."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import time
import json

from aiohttp import web

from insight_capture.api.support import read_json_body
from insight_capture.runtime.camera_health import evaluate_calibration_cameras


class MobileCaptureRoutes:
    def __init__(self, context, recording):
        self.context = context
        self.recording = recording
        self._last_preflight = {}
        self._task = None
        self._operation = None
        self._commands = OrderedDict()
        self._last_status = {}
        self._next_discovery = 0.0
        self._command_lock = asyncio.Lock()

    def _snapshot(self):
        manager = self.context.recording_manager
        busy = self._task is not None and not self._task.done()
        status = self._last_status if busy else manager.status()
        if not busy:
            self._last_status = status
        if busy:
            state = "starting" if self._operation["action"] == "start" else "saving"
        elif status.get("merge_state") in {"finalizing", "merging"}:
            state = "saving"
        elif status.get("recording"):
            state = "recording"
        elif status.get("merge_state") == "error":
            state = "error"
        elif self._operation and self._operation.get("error"):
            state = "error"
        elif status.get("merge_state") == "done":
            state = "saved"
        else:
            state = "idle"
        task_status = self.context.take_store.task_status()
        if not busy and not status.get("recording") and state != "saving":
            now = time.monotonic()
            refresh = now >= self._next_discovery
            self._last_preflight = self.context.capture_preflight.evaluate(
                refresh_topics=refresh, require_global_pose=False
            )
            if not task_status.get("active"):
                self._last_preflight["failures"].append({
                    "code": "task_inactive", "message": "请先在任务管理中选择数采任务。"
                })
                self._last_preflight["ok"] = False
            if refresh:
                self._next_discovery = now + 10.0
            report = self._last_preflight
        else:
            health, _ = evaluate_calibration_cameras(
                self.context.node, self.context.capture_preflight.camera_stale_sec
            )
            report = {**self._last_preflight, "camera_health": health}
        role_names = {"head": "头部", "right_hand": "腕部 A", "left_hand": "腕部 B"}
        devices = [{"name": pose.name, "role": pose.teleop_role,
                    "label": role_names[pose.teleop_role] + " · " + pose.name.split("_")[0].upper()}
                   for pose in self.context.node.poses]
        return {
            "state": state, "recording": bool(status.get("recording")),
            "recording_mode": status.get("recording_mode"),
            "output_path": status.get("output_path"),
            "started_at_epoch_s": getattr(manager, "started_at", None),
            "server_time": time.time(), "operation": self._operation,
            "preflight": report, "devices": devices, "task_status": task_status,
            "preview_cameras": [{"name": c.name, "label": c.label} for c in self.context.node.cameras],
            "distributed": status.get("distributed"),
            "storage": status.get("storage", {}),
            "error": status.get("merge_error"),
        }

    async def status(self, request):
        if request.query.get("preview") == "1":
            self.context.node.note_viewer_activity()
        return web.json_response(await asyncio.to_thread(self._snapshot), headers={"Cache-Control": "no-store"})

    async def command(self, request):
        async with self._command_lock:
            return await self._accept(request)

    async def _accept(self, request):
        origin = request.headers.get("Origin")
        if origin and origin != f"{request.scheme}://{request.host}":
            raise web.HTTPForbidden(text="Cross-origin capture commands are not allowed")
        body = await read_json_body(request)
        command_id = str(body.get("command_id", ""))
        if not command_id or len(command_id) > 100:
            raise ValueError("command_id is required (maximum 100 characters)")
        action = request.match_info["action"]
        if action not in {"start", "stop"}:
            raise web.HTTPNotFound()
        fingerprint = (action, body.get("output_path"))
        previous = self._commands.get(command_id)
        if previous is not None:
            if previous[0] != fingerprint:
                raise RuntimeError("同一个命令编号不能用于不同操作。")
            return web.json_response(previous[1], status=202)
        if self._task is not None and not self._task.done():
            raise RuntimeError("上一项操作仍在处理，请等待状态更新。")
        manager = self.context.recording_manager
        status = await asyncio.to_thread(manager.status)
        if status.get("merge_state") in {"finalizing", "merging"}:
            raise RuntimeError("正在保存上一条采集。")
        if action == "start" and status.get("recording"):
            raise RuntimeError("已有采集正在进行。")
        if action == "stop":
            if not status.get("recording") or status.get("recording_mode") != "capture":
                raise RuntimeError("当前没有正在进行的采集。")
            if not body.get("output_path") or body["output_path"] != status.get("output_path"):
                raise RuntimeError("采集已变化，请刷新状态后再停止。")
        self._last_status = status
        self._operation = {"command_id": command_id, "action": action, "state": "running", "error": None}
        self._commands[command_id] = (fingerprint, self._operation)
        while len(self._commands) > 100:
            self._commands.popitem(last=False)
        self._task = asyncio.create_task(self._run(action, self._operation, status.get("output_path")))
        return web.json_response(self._operation, status=202)

    async def _run(self, action, operation, output_path):
        try:
            if action == "start":
                response = await self.recording._start_with_preflight(
                    topics=None, bag_name=None, automation=False, relaxed_preflight=True
                )
            else:
                response = await self.recording.stop_recording(
                    expected_recording=(output_path, "capture")
                )
            result = json.loads(response.body)
            if response.status >= 400:
                raise RuntimeError(result.get("speech") or result["error"])
            if result.get("merge_state") == "error":
                raise RuntimeError(result.get("merge_error") or "录制文件保存失败。")
            if action == "start":
                self._last_preflight = result["preflight"]
            operation["state"] = "complete"
        except Exception as exc:
            operation.update(state="error", error=str(exc))
            self.context.node.get_logger().error(f"Mobile capture {action} failed: {exc}")

    async def shutdown(self, _app):
        # A disconnected browser must not cancel a writer start or its finalization.
        if self._task is not None:
            await asyncio.shield(self._task)
