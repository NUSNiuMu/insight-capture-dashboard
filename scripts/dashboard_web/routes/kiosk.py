"""Ephemeral state shared by the split on-device kiosk renderers."""

from aiohttp import web

from dashboard_web.context import DashboardContext
from dashboard_web.support import read_json_body


class KioskRoutes:
    def __init__(self, context: DashboardContext) -> None:
        self.context = context

    async def _handle_state_get(self, _request: web.Request) -> web.Response:
        return web.json_response(dict(self.context.kiosk_state))

    async def _handle_state_post(self, request: web.Request) -> web.Response:
        payload = await read_json_body(request)
        if "capture_performance" in payload:
            self.context.kiosk_state["capture_performance"] = bool(
                payload["capture_performance"]
            )
        return web.json_response(dict(self.context.kiosk_state))
