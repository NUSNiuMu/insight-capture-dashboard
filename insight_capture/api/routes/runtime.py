"""Headless capture preflight and system status routes."""

from __future__ import annotations

import asyncio
from aiohttp import web

from insight_capture.api.context import DashboardContext


class RuntimeRoutes:
    def __init__(self, context: DashboardContext) -> None:
        self.context = context

    async def _handle_preflight(self, _request: web.Request) -> web.Response:
        report = await asyncio.to_thread(self.context.capture_preflight.evaluate)
        report["message"] = self.context.capture_preflight.message(report)
        return web.json_response(report, status=200 if report["ok"] else 422)

    async def _handle_system_status(self, _request: web.Request) -> web.Response:
        report = await asyncio.to_thread(self.context.capture_preflight.evaluate)
        return web.json_response({
            "type": "system_status",
            "ready": bool(report.get("ok")),
            "message": self.context.capture_preflight.message(report),
            "preflight": report,
            "recording": self.context.recording_manager.status(),
            "session": self.context.take_store.snapshot(),
            "runtime": self.context.node.preview_status(),
        })
