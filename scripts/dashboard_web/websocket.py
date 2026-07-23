"""Pose WebSocket clients and broadcast lifecycle."""

import asyncio
import contextlib
import json
from typing import Set

from aiohttp import web

from .context import DashboardContext


class PoseWebSocketService:
    def __init__(self, context: DashboardContext) -> None:
        self.context = context
        self.clients: Set[web.WebSocketResponse] = set()

    async def _on_startup(self, app: web.Application) -> None:
        app["broadcast_task"] = asyncio.create_task(self._broadcast_loop())

    async def _on_shutdown(self, app: web.Application) -> None:
        task = app.get("broadcast_task")
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.context.recording_manager.stop()

    def _build_pose_broadcast_json(self) -> str:
        payload = self.context.node.build_pose_payload()
        for pose in payload["poses"]:
            pose["asset_url"] = self.context.node.model_asset_url(pose.get("avatar_model"))
        return json.dumps(payload)

    async def _broadcast_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(1.0 / self.context.node.pose_publish_hz)
            if not self.clients:
                continue
            # build_pose_payload() rounds/serializes a full ~300-point trace
            # per pose every tick -- CPU-bound Python that used to run
            # in-line on this event loop and block pending camera-frame HTTP
            # responses for its duration. Running it (plus the json.dumps,
            # so send_str below doesn't re-serialize) in the default executor
            # keeps the loop free to answer other requests while it computes.
            payload_json = await loop.run_in_executor(None, self._build_pose_broadcast_json)
            stale = []
            for ws in list(self.clients):
                try:
                    await ws.send_str(payload_json)
                except Exception:
                    stale.append(ws)
            for ws in stale:
                self.clients.discard(ws)

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20.0)
        await ws.prepare(request)
        self.clients.add(ws)
        snapshot = self.context.node.build_pose_payload()
        for pose in snapshot["poses"]:
            pose["asset_url"] = self.context.node.model_asset_url(pose.get("avatar_model"))
        await ws.send_json(snapshot)
        async for _message in ws:
            pass
        self.clients.discard(ws)
        return ws
