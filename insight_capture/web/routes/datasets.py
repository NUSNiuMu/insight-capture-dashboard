"""UMI dataset export HTTP handlers."""

from __future__ import annotations

from aiohttp import web

from insight_capture.web.context import DashboardContext
from insight_capture.web.support import read_json_body


class UmiExportRoutes:
    def __init__(self, context: DashboardContext) -> None:
        self.context = context

    async def _handle_start(self, request: web.Request) -> web.Response:
        body = await read_json_body(request)
        bag_names = body.get("bag_names", [])
        if not isinstance(bag_names, list) or not all(
            isinstance(name, str) for name in bag_names
        ):
            raise ValueError("bag_names must be a list of strings")
        camera_names = body.get("camera_names")
        if camera_names is not None and (
            not isinstance(camera_names, list)
            or not all(isinstance(name, str) for name in camera_names)
        ):
            raise ValueError("camera_names must be a list of strings")
        try:
            payload = self.context.umi_export_manager.start(
                bag_names,
                str(body.get("image_mode", "original")),
                camera_names,
                str(body.get("episode_mode", "bag")),
                str(body.get("export_format", "umi")),
                str(body.get("task", "")),
            )
        except RuntimeError as exc:
            return web.json_response({"error": str(exc)}, status=409)
        except FileNotFoundError as exc:
            return web.json_response({"error": str(exc)}, status=404)
        return web.json_response(payload)

    async def _handle_status(self, _request: web.Request) -> web.Response:
        return web.json_response(self.context.umi_export_manager.status())

    async def _on_shutdown(self, _app: web.Application) -> None:
        self.context.umi_export_manager.stop()
