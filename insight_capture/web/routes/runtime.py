"""Headless capture preflight, system status, and voice-alert routes."""

from __future__ import annotations

from aiohttp import web

from insight_capture.web.context import DashboardContext


class RuntimeRoutes:
    def __init__(self, context: DashboardContext) -> None:
        self.context = context

    async def _handle_preflight(self, _request: web.Request) -> web.Response:
        report = self.context.capture_preflight.evaluate()
        report["speech"] = self.context.capture_preflight.speech(report)
        return web.json_response(report, status=200 if report["ok"] else 422)

    async def _handle_system_status(self, _request: web.Request) -> web.Response:
        report = self.context.capture_preflight.evaluate()
        return web.json_response({
            "type": "system_status",
            "ready": bool(report.get("ok")),
            "speech": self.context.capture_preflight.speech(report),
            "preflight": report,
            "recording": self.context.recording_manager.status(),
            "session": self.context.take_store.snapshot(),
            "runtime": self.context.node.preview_status(),
        })

    async def _handle_voice_alerts(self, request: web.Request) -> web.Response:
        try:
            cursor = max(0, int(request.query.get("cursor", "0")))
        except ValueError:
            raise ValueError("cursor must be an integer")
        alerts = self.context.voice_alerts.since(cursor)
        return web.json_response({
            "type": "voice_alerts",
            "alerts": alerts,
            "cursor": alerts[-1]["id"] if alerts else cursor,
        })
