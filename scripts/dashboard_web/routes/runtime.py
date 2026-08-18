"""Headless capture runtime, Session/Take, and voice-alert routes."""

from __future__ import annotations

from aiohttp import web

from dashboard_web.context import DashboardContext
from dashboard_web.support import read_json_body


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

    async def _handle_sessions(self, _request: web.Request) -> web.Response:
        return web.json_response(self.context.take_store.snapshot())

    async def _handle_reject_current(self, request: web.Request) -> web.Response:
        payload = await read_json_body(request)
        reason = str(payload.get("reason") or "operator_rejected")
        try:
            take = self.context.take_store.reject_current(reason)
        except RuntimeError:
            return web.json_response({
                "type": "take_decision",
                "ok": False,
                "speech": "当前没有可作废的记录。",
            }, status=409)
        return web.json_response({
            "type": "take_decision",
            "ok": True,
            "speech": f"第{take['take_id']}条已标记作废，原始数据已保留。",
            "take": take,
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
