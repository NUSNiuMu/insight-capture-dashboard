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
        self._trace_cursor = None

    async def _on_startup(self, app: web.Application) -> None:
        app["broadcast_task"] = asyncio.create_task(self._broadcast_loop())

    async def _on_shutdown(self, app: web.Application) -> None:
        task = app.get("broadcast_task")
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.context.recording_manager.stop()

    def _discard_client(self, ws: web.WebSocketResponse) -> None:
        """Release one viewer lease exactly once, including send failures."""
        if ws not in self.clients:
            return
        self.clients.discard(ws)
        self.context.node.viewer_disconnected()

    def _build_pose_broadcast_json(self) -> str:
        payload = self.context.node.build_pose_payload(trace_cursor=self._trace_cursor)
        self._trace_cursor = {
            "generation": payload["trace_generation"],
            "sequences": {
                pose["name"]: pose["trace_update"]["to_seq"]
                for pose in payload["poses"]
            },
        }
        for pose in payload["poses"]:
            pose["asset_url"] = self.context.node.model_asset_url(
                pose.get("avatar_model")
            )
        return json.dumps(payload)

    async def _broadcast_loop(self) -> None:
        loop = asyncio.get_running_loop()
        publish_interval = 1.0 / self.context.node.pose_publish_hz
        next_publish_at = loop.time() + publish_interval
        while True:
            await asyncio.sleep(max(0.0, next_publish_at - loop.time()))
            next_publish_at += publish_interval
            if not self.clients:
                next_publish_at = loop.time() + publish_interval
                continue
            # Build the CPU-heavy trace payload off the event loop.
            payload_json = await loop.run_in_executor(None, self._build_pose_broadcast_json)
            stale = []
            for ws in list(self.clients):
                try:
                    await ws.send_str(payload_json)
                except Exception:
                    stale.append(ws)
            for ws in stale:
                self._discard_client(ws)
                await ws.close()
            if loop.time() >= next_publish_at:
                # Drop missed deadlines instead of bursting stale pose frames.
                next_publish_at = loop.time() + publish_interval

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20.0)
        await ws.prepare(request)
        self.clients.add(ws)
        self.context.node.viewer_connected()
        snapshot = self.context.node.build_pose_payload()
        for pose in snapshot["poses"]:
            pose["asset_url"] = self.context.node.model_asset_url(
                pose.get("avatar_model")
            )
        await ws.send_json(snapshot)
        try:
            async for _message in ws:
                pass
        finally:
            self._discard_client(ws)
        return ws
