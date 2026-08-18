"""Sparse mapping status and reset routes."""

from aiohttp import web

from ..context import DashboardContext


class MappingRoutes:
    def __init__(self, context: DashboardContext) -> None:
        self.context = context

    async def _handle_snapshot(self, _request: web.Request) -> web.Response:
        return web.json_response(self.context.node.build_mapping_payload())

    async def _handle_reset(self, _request: web.Request) -> web.Response:
        payload = self.context.node.reset_mapping()
        return web.json_response(payload, status=200 if payload["ok"] else 503)
