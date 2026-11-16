"""Sparse mapping status and reset routes."""

import asyncio

from aiohttp import web

from ..context import DashboardContext, capture_transition_lock
from insight_capture.runtime.camera_health import evaluate_calibration_cameras


_CAMERA_DISPLAY_NAMES = {
    "insight3_a": "右手相机",
    "insight3_b": "左手相机",
    "insight9_a": "头部相机",
}


def _camera_failure_message(unavailable: list[dict]) -> str:
    details = []
    for item in unavailable:
        camera_name = str(item.get("camera"))
        name = _CAMERA_DISPLAY_NAMES.get(camera_name, camera_name)
        missing = set(item.get("missing") or [])
        if "configuration" in missing:
            details.append(f"{name}未配置")
        elif {"image", "vio"}.issubset(missing):
            details.append(f"{name}没有图像和VIO数据")
        elif "image" in missing:
            details.append(f"{name}没有图像数据")
        else:
            details.append(f"{name}没有VIO数据")
    return "无法开始校准：" + "；".join(details) + "。请检查相机连接。"


class MappingRoutes:
    def __init__(self, context: DashboardContext) -> None:
        self.context = context
        self._transition = capture_transition_lock(context)

    async def _handle_snapshot(self, _request: web.Request) -> web.Response:
        return web.json_response(self.context.node.build_mapping_payload())

    async def _handle_reset(self, _request: web.Request) -> web.Response:
        if self._transition.locked():
            return self._busy_response()
        cancelled = False
        async with self._transition:
            recording = await asyncio.to_thread(self.context.recording_manager.status)
            if (recording.get("recording") and recording.get("recording_mode") != "capture"
                    or recording.get("merge_state") in {"finalizing", "merging"}
                    or (recording.get("distributed") or {}).get("phase") == "starting"):
                return self._busy_response()
            stale_sec = max(
                0.2,
                float(getattr(self.context.node, "camera_stale_timeout_sec", 2.0)),
            )
            health, unavailable = evaluate_calibration_cameras(
                self.context.node,
                stale_sec,
            )
            head = next(pose.name for pose in self.context.node.poses if pose.teleop_role == "head")
            unavailable = [item for item in unavailable if item["camera"] == head]
            if unavailable:
                return web.json_response(
                    {
                        "ok": False,
                        "error": "calibration_cameras_not_ready",
                        "message": _camera_failure_message(unavailable),
                        "camera_health": health,
                        "unavailable_cameras": unavailable,
                    },
                    status=409,
                )

            loop = asyncio.get_running_loop()
            dispatched = loop.create_future()

            def mark_dispatched():
                if not dispatched.done():
                    dispatched.set_result(None)

            def signal_dispatched():
                if not loop.is_closed():
                    loop.call_soon_threadsafe(mark_dispatched)

            work = asyncio.create_task(asyncio.to_thread(
                self.context.node.reset_mapping, on_dispatched=signal_dispatched
            ))

            def completed(task):
                signal_dispatched()
                if not task.cancelled():
                    task.exception()  # Consume errors if the HTTP caller disconnected.

            work.add_done_callback(completed)
            while not dispatched.done():
                try:
                    await asyncio.shield(dispatched)
                except asyncio.CancelledError:
                    # A cancelled HTTP request cannot leave an undispatched reset
                    # free to race a newly admitted capture operation.
                    cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        payload = await asyncio.shield(work)
        if not payload["ok"]:
            payload["message"] = "校准服务未就绪，请检查建图和重定位服务。"
        return web.json_response(payload, status=200 if payload["ok"] else 503)

    @staticmethod
    def _busy_response() -> web.Response:
        return web.json_response(
            {"error": "recording_busy", "message": "正在准备、停止、保存或专用标定录制，请结束当前操作后再校准。"},
            status=409,
        )
