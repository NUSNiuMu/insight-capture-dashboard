"""AlignmentRoutes HTTP handlers."""

from __future__ import annotations

from aiohttp import web

from dashboard_web.context import DashboardContext


class AlignmentRoutes:
    def __init__(self, context: DashboardContext) -> None:
        self.context = context

    async def _handle_alignment_snapshot(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "type": "alignment_status",
                "alignment": self.context.node.build_alignment_payload(),
            }
        )

    async def _handle_alignment_start(self, _request: web.Request) -> web.Response:
        status_text = self.context.node.start_live_alignment()
        return web.json_response(
            {
                "ok": bool(self.context.node.live_alignment_active),
                "type": "alignment_status",
                "status_text": status_text,
                "alignment": self.context.node.build_alignment_payload(),
            }
        )

    async def _handle_alignment_stop(self, _request: web.Request) -> web.Response:
        status_text = self.context.node.stop_live_alignment()
        return web.json_response(
            {
                "ok": True,
                "type": "alignment_status",
                "status_text": status_text,
                "alignment": self.context.node.build_alignment_payload(),
            }
        )
