"""Sparse mapping status and reset routes."""

from aiohttp import web

from ..context import DashboardContext
from insight_capture.runtime.camera_health import evaluate_calibration_cameras


_CAMERA_SPEECH_NAMES = {
    "insight3_a": "右手相机",
    "insight3_b": "左手相机",
    "insight9_a": "头部相机",
}


def _camera_failure_speech(unavailable: list[dict]) -> str:
    details = []
    for item in unavailable:
        camera_name = str(item.get("camera"))
        name = _CAMERA_SPEECH_NAMES.get(camera_name, camera_name)
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

    async def _handle_snapshot(self, _request: web.Request) -> web.Response:
        return web.json_response(self.context.node.build_mapping_payload())

    async def _handle_reset(self, _request: web.Request) -> web.Response:
        if not getattr(self.context.node, "fake_pose", False):
            stale_sec = max(
                0.2,
                float(getattr(self.context.node, "camera_stale_timeout_sec", 2.0)),
            )
            health, unavailable = evaluate_calibration_cameras(
                self.context.node,
                stale_sec,
            )
            if unavailable:
                return web.json_response(
                    {
                        "ok": False,
                        "error": "calibration_cameras_not_ready",
                        "speech": _camera_failure_speech(unavailable),
                        "camera_health": health,
                        "unavailable_cameras": unavailable,
                    },
                    status=409,
                )
        payload = self.context.node.reset_mapping()
        if not payload["ok"]:
            payload["speech"] = "校准服务未就绪，请检查建图和重定位服务。"
        return web.json_response(payload, status=200 if payload["ok"] else 503)
