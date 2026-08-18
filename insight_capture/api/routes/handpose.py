"""Offline hand-pose HTTP handlers."""

from __future__ import annotations

from aiohttp import web

from insight_capture.api.context import DashboardContext
from insight_capture.api.support import read_json_body


class HandPoseRoutes:
    def __init__(self, context: DashboardContext) -> None:
        self.context = context

    async def _handle_capabilities(self, _request: web.Request) -> web.Response:
        return web.json_response(self.context.handpose_manager.capabilities())

    async def _handle_status(self, _request: web.Request) -> web.Response:
        payload = self.context.handpose_manager.status()
        payload["results"] = self.context.handpose_manager.list_results()
        return web.json_response(payload)

    async def _handle_start(self, request: web.Request) -> web.Response:
        body = await read_json_body(request)
        bag_name = str(body.get("bag_name", "")).strip()
        method = str(body.get("method", "")).strip()
        if not bag_name:
            raise ValueError("bag_name is required")
        if not method:
            raise ValueError("method is required")
        self.context.handpose_manager.start(bag_name, method)
        return web.json_response(self.context.handpose_manager.status())

    async def _handle_stop(self, _request: web.Request) -> web.Response:
        self.context.handpose_manager.stop()
        return web.json_response(self.context.handpose_manager.status())

    async def _handle_result(self, request: web.Request) -> web.StreamResponse:
        bag_name = request.query.get("bag_name", "").strip()
        method = request.query.get("method", "").strip()
        try:
            path = self.context.handpose_manager.result_path(bag_name, method)
        except FileNotFoundError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc
        response = web.FileResponse(path)
        response.headers["Cache-Control"] = "no-store"
        return response

    async def _handle_preview(self, request: web.Request) -> web.StreamResponse:
        bag_name = request.query.get("bag_name", "").strip()
        method = request.query.get("method", "").strip()
        try:
            path = self.context.handpose_manager.preview_path(bag_name, method)
        except FileNotFoundError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc
        return web.FileResponse(path)

    async def _on_shutdown(self, _app: web.Application) -> None:
        self.context.handpose_manager.stop()
