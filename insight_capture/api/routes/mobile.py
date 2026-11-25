"""Phone controls using the shared capture and distributed recording lifecycle."""

from __future__ import annotations

import asyncio
import time
import json
from pathlib import Path
from threading import Lock
from insight_capture.runtime.discovery import resolved_cameras, startup_ready

from aiohttp import web

from insight_capture.media.preview_config import preview_description
from insight_capture.api.support import is_nvme_filesystem, read_disk_space, read_json_body
from insight_capture.runtime.camera_health import evaluate_calibration_cameras
from insight_capture.runtime.mobile_commands import MobileCommandStore


def _live_alerts(take, output_path, state):
    """Project open QC events for this recording, never a previous Take."""
    if (state not in {"recording", "saving"} or not output_path
            or not isinstance(take, dict) or take.get("bag_path") != output_path
            or take.get("state") not in {"recording", "finalizing"}):
        return []
    timeline = take.get("anomaly_timeline")
    if not isinstance(timeline, list):
        return []
    active = {}
    for event in timeline:
        if not isinstance(event, dict):
            continue
        code = event.get("code")
        if not isinstance(code, str) or not code:
            continue
        if event.get("event") == "resolved":
            active.pop(code, None)
        elif event.get("event") == "started" and isinstance(event.get("message"), str):
            active[code] = {key: event.get(key) for key in (
                "code", "severity", "message", "epoch_s", "details"
            )}
    return sorted(active.values(), key=lambda item: item["severity"] != "critical")


class MobileCaptureRoutes:
    def __init__(self, context, recording):
        self.context = context
        self.recording = recording
        self._last_preflight = {}
        self._task = None
        self._operation = None
        self._commands = MobileCommandStore(context.results_root / "mobile_commands.sqlite3")
        self._last_status = {}
        self._next_discovery = 0.0
        self._preflight_lock = Lock()
        self._command_lock = asyncio.Lock()

    @staticmethod
    def _camera_inputs_ready(report):
        cameras = report.get("camera_health") or {}
        clocks = report.get("clocks") or (report.get("distributed") or {}).get("clocks") or {}
        return (
            len(cameras) == 3
            and all(camera.get("ready") for camera in cameras.values())
            and bool(clocks.get("ok"))
            and bool((report.get("image_clocks") or {}).get("ok"))
        )

    def _idle_preflight(self, task_active):
        with self._preflight_lock:
            now = time.monotonic()
            refresh = now >= self._next_discovery
            evaluate = self.context.capture_preflight.evaluate
            report = evaluate(refresh_topics=refresh, require_global_pose=False)
            if self._camera_inputs_ready(report):
                previous_topics = self._last_preflight.get("topics") or {}
                recovery = previous_topics.get("recovery_refresh")
                if not refresh and not self._camera_inputs_ready(self._last_preflight):
                    # The graph cached during a dropout can outlive the first live frames.
                    missing_before = report["topics"]["missing"]
                    started = time.monotonic()
                    report = evaluate(refresh_topics=True, require_global_pose=False)
                    recovery = {
                        "missing_before": missing_before,
                        "missing_after": report["topics"]["missing"],
                        "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
                    }
                    refresh = True
                if recovery:
                    report["topics"] = {**report["topics"], "recovery_refresh": recovery}
            if refresh:
                self._next_discovery = now + 10.0
            if not task_active:
                report["failures"].append({"code": "task_inactive", "message": "请先创建或选择任务"})
                report["ok"] = False
            self._last_preflight = report
            return report

    def _snapshot(self):
        manager = self.context.recording_manager
        busy = self._task is not None and not self._task.done()
        status = self._last_status if busy else manager.status()
        playing = self.context.prepared_playback_manager.status()["state"] == "playing"
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
        elif status.get("merge_state") in {"saved", "done"}:
            state = "saved"
        else:
            state = "idle"
        task_status = self.context.take_store.task_status()
        current_take = self.context.take_store.current()
        live_alerts = _live_alerts(current_take, status.get("output_path"), "playback" if playing else state)
        if current_take is not None:
            current_take = {key: current_take.get(key) for key in (
                "take_id", "task_id", "state", "bag_path", "start_epoch_s", "end_epoch_s", "recording_timing"
            )}
        timing = dict(status.get("recording_timing") or {}) or None
        # The saving worker may be draining/flushing with _last_status frozen.
        # Freeze the provisional timer at stop acceptance until the saved file
        # provides its authoritative data span.
        if busy and self._operation["action"] == "stop" and timing and timing.get("started_at_epoch_s") is not None:
            stopped = self._operation["accepted_at_epoch_s"]
            timing.update(stopped_at_epoch_s=stopped, duration_s=max(0.0, stopped - timing["started_at_epoch_s"]))
        distributed = status.get("distributed")
        if playing:
            state = "playback"
            report = {**self._last_preflight, "ok": False, "failures": [], "camera_health": {}}
        elif not busy and not status.get("recording") and state != "saving":
            report = self._idle_preflight(task_status.get("active"))
            clock = (report.get("distributed") or {}).get("clocks")
            if distributed is not None and clock is not None:
                distributed = {**distributed, "clock": {**(distributed.get("clock") or {}), **clock}}
        else:
            health, _ = evaluate_calibration_cameras(
                self.context.node, self.context.capture_preflight.camera_stale_sec
            )
            report = {**self._last_preflight, "camera_health": health}
            monitor = getattr(manager, "clock_monitor", None)
            if monitor:
                report["clocks"] = monitor.status()
            pipeline = getattr(self.context.node, "_image_pipeline", None)
            if pipeline:
                report["image_clocks"] = pipeline.clock_status()
        phone_time = dict(getattr(self.context, "phone_time_sync", {}))
        camera_time = dict(getattr(self.context, "camera_time_sync", {}))
        if phone_time.get("state") in {"syncing", "error"}:
            message = "正在同步手机时间，请稍候。" if phone_time["state"] == "syncing" else "Jetson 时间同步失败。"
            report = {**report, "ok": False, "failures": [*report.get("failures", []),
                       {"code": "phone_time_sync", "message": message}]}
        if camera_time.get("state") == "syncing":
            report = {**report, "ok": False, "failures": [*report.get("failures", []),
                       {"code": "camera_time_sync", "message": "正在校时"}]}
        # Admission checks are a start-time snapshot; capacity is live operator
        # information and must keep decreasing while the writer is running.
        recording_root = manager.rosbag_root
        recording_space = read_disk_space(recording_root)
        nvme_space = None
        if is_nvme_filesystem(recording_root):
            nvme_space = recording_space
        elif is_nvme_filesystem(self.context.results_root):
            nvme_space = read_disk_space(self.context.results_root)
        report = {**report, "storage": {
            **(report.get("storage") or {}),
            **recording_space,
        }}
        role_names = {"head": "Head", "right_hand": "Wrist A", "left_hand": "Wrist B"}
        devices = [{"name": pose.name, "role": pose.teleop_role,
                    "label": role_names.get(pose.teleop_role, pose.teleop_role) + " · " + pose.name.split("_")[0].upper()}
                   for pose in self.context.node.poses]
        heads = {pose.name for pose in self.context.node.poses if pose.teleop_role == "head"}
        preview_cameras = sorted(self.context.node.cameras, key=lambda camera: camera.device_name not in heads)
        try:
            links = resolved_cameras()
        except (OSError, ValueError, KeyError):
            links = []
        return {
            "system": {"managed_camera_startup": bool(getattr(manager, "clock_monitor", None) and manager.clock_monitor.config.get("continuous_camera_clock")), "uptime_s": float(Path('/proc/uptime').read_text().split()[0]),
                       "camera_links": links, "camera_startup": startup_ready()},
            "state": state, "recording": bool(status.get("recording")),
            "recording_mode": status.get("recording_mode"),
            "output_path": status.get("output_path"),
            "started_at_epoch_s": getattr(manager, "started_at", None),
            "recording_timing": timing,
            "server_time": time.time(), "time_sync": phone_time, "camera_time_sync": camera_time,
            "operation": self._operation,
            "preflight": report, "devices": devices, "task_status": task_status,
            "current_take": current_take, "live_alerts": live_alerts,
            "preview": preview_description(self.context.node.cameras, self.context.node.poses, paused=self.context.node._preview_manager.paused),
            "preview_cameras": [{"name": c.name, "label": c.label} for c in preview_cameras],
            "distributed": distributed,
            "storage": status.get("storage", {}),
            "nvme_storage": nvme_space,
            "error": status.get("merge_error"),
        }

    async def status(self, request):
        return web.json_response(await asyncio.to_thread(self._snapshot), headers={"Cache-Control": "no-store"})

    async def command(self, request):
        async with self._command_lock:
            acceptance = asyncio.create_task(self._accept(request))
            try:
                return await asyncio.shield(acceptance)
            except asyncio.CancelledError:
                # Persisting the receipt and scheduling its action must finish
                # together, even when the HTTP client disappears in between.
                await acceptance
                raise

    async def command_status(self, request):
        previous = await asyncio.to_thread(self._commands.get, request.match_info["command_id"])
        return web.json_response({"operation": previous[1] if previous else None},
                                 headers={"Cache-Control": "no-store"})

    async def _accept(self, request):
        origin = request.headers.get("Origin")
        if origin and origin != f"{request.scheme}://{request.host}":
            raise web.HTTPForbidden(text="Cross-origin capture commands are not allowed")
        body = await read_json_body(request)
        command_id = body.get("command_id")
        if not isinstance(command_id, str) or not command_id.strip() or len(command_id) > 100:
            raise ValueError("command_id is required (maximum 100 characters)")
        action = request.match_info["action"]
        if action not in {"start", "stop"}:
            raise web.HTTPNotFound()
        fingerprint = (action, body.get("output_path"))
        previous = await asyncio.to_thread(self._commands.get, command_id)
        if previous is not None:
            if previous[0] != fingerprint:
                raise RuntimeError("A command ID cannot be reused for a different operation.")
            if (action == "start" and previous[1].get("task_id") is not None
                    and previous[1]["task_id"] != body.get("task_id")):
                raise RuntimeError("A command ID cannot be reused for a different task.")
            return web.json_response(previous[1], status=202)
        task_id = body.get("task_id") if action == "start" else None
        if action == "start":
            if not isinstance(task_id, str) or not task_id.strip():
                raise ValueError("页面已更新，请刷新后再开始录制。")
            current = self.context.take_store.task_status()
            if (not current.get("active")
                    or (current.get("task") or {}).get("task_id") != task_id):
                raise RuntimeError("采集任务已变更，请确认当前任务后再开始录制。")
        if self._task is not None and not self._task.done():
            raise RuntimeError("An operation is still running. Wait for it to finish.")
        manager = self.context.recording_manager
        status = await asyncio.to_thread(manager.status)
        if status.get("merge_state") in {"finalizing", "merging"}:
            raise RuntimeError("Wait for the previous recording to finish saving.")
        if action == "start" and status.get("recording"):
            raise RuntimeError("A recording is already running.")
        if action == "stop":
            if not status.get("recording") or status.get("recording_mode") != "capture":
                raise RuntimeError("No capture recording is running.")
            if not body.get("output_path") or body["output_path"] != status.get("output_path"):
                raise RuntimeError("The recording has changed. Refresh before stopping it.")
        operation = {"command_id": command_id, "action": action, "task_id": task_id, "state": "running", "error": None, "accepted_at_epoch_s": time.time()}
        await asyncio.to_thread(self._commands.reserve, fingerprint, operation)
        self._last_status = status
        self._operation = operation
        self._task = asyncio.create_task(self._run(action, self._operation, status.get("output_path")))
        return web.json_response(self._operation, status=202)

    async def _run(self, action, operation, output_path):
        try:
            if action == "start":
                response = await self.recording._start_with_preflight(
                    topics=None, bag_name=None, relaxed_preflight=True,
                    expected_task_id=operation["task_id"],
                )
            else:
                response = await self.recording.stop_recording(
                    expected_recording=(output_path, "capture")
                )
            result = json.loads(response.body)
            if response.status >= 400:
                raise RuntimeError(result.get("message") or result["error"])
            if result.get("merge_state") == "error":
                raise RuntimeError(result.get("merge_error") or "Could not save the recording.")
            if action == "start":
                self._last_preflight = result["preflight"]
            operation.update(state="complete", output_path=result.get("output_path"))
        except Exception as exc:
            operation.update(state="error", error=str(exc))
            self.context.node.get_logger().error(f"Mobile capture {action} failed: {exc}")
        finally:
            operation["finished_at_epoch_s"] = time.time()
            try:
                await asyncio.to_thread(self._commands.finish, operation)
            except Exception as exc:
                # A receipt write failure cannot undo an already completed
                # capture/save. The committed running receipt prevents replay;
                # a restart will expose its result as interrupted/uncertain.
                self.context.node.get_logger().error(f"Could not persist mobile command result: {exc}")

    async def shutdown(self, _app):
        # A disconnected browser must not cancel a writer start or its finalization.
        async with self._command_lock:
            pass
        if self._task is not None:
            await asyncio.shield(self._task)
