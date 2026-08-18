"""Session metadata routes."""

from aiohttp import web

from insight_capture.web.context import DashboardContext


class SessionRoutes:
    def __init__(self, context: DashboardContext) -> None:
        self.context = context

    async def _handle_list(self, _request: web.Request) -> web.Response:
        return web.json_response(self.context.take_store.snapshot())
