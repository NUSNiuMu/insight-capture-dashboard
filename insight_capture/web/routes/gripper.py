"""Offline gripper extraction HTTP handlers."""

from __future__ import annotations

from aiohttp import web

from insight_capture.web.context import DashboardContext
from insight_capture.web.support import read_json_body


class GripperExtractionRoutes:
    def __init__(self, context: DashboardContext) -> None:
        self.context = context

    async def _handle_start(self, request: web.Request) -> web.Response:
        body = await read_json_body(request)
        try:
            payload = self.context.gripper_extraction_manager.start(
                str(body.get("bag_name", "")),
                str(body.get("camera_name", "")),
                str(body.get("topic", "")).strip(),
                require_calibration=bool(body.get("require_calibration", True)),
            )
        except RuntimeError as exc:
            return web.json_response({"error": str(exc)}, status=409)
        except FileNotFoundError as exc:
            return web.json_response({"error": str(exc)}, status=404)
        return web.json_response(payload)

    async def _handle_status(self, _request: web.Request) -> web.Response:
        return web.json_response(self.context.gripper_extraction_manager.status())

    async def _handle_result(self, request: web.Request) -> web.StreamResponse:
        try:
            path = self.context.gripper_extraction_manager.result_path(
                request.query.get("bag_name", ""),
                request.query.get("camera_name", ""),
            )
        except FileNotFoundError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc
        response = web.FileResponse(path)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Disposition"] = f'attachment; filename="{path.name}"'
        return response

    async def _on_shutdown(self, _app: web.Application) -> None:
        self.context.gripper_extraction_manager.stop()
