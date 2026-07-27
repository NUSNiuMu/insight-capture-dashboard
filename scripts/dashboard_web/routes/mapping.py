"""Sparse mapping status, reset, and visualization WebSocket routes."""

import asyncio
import contextlib
import json

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

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20.0)
        await ws.prepare(request)
        map_version = None
        try:
            while not ws.closed:
                payload = self.context.node.build_mapping_payload(
                    known_map_version=map_version
                )
                map_version = int(payload["map_version"])
                await ws.send_str(json.dumps(payload, separators=(",", ":")))
                await asyncio.sleep(0.05)
        except (ConnectionError, RuntimeError):
            pass
        finally:
            with contextlib.suppress(Exception):
                await ws.close()
        return ws
