"""Loopback-only binary video transport for the local Qt client."""

import asyncio
import contextlib
import ipaddress

from aiohttp import web


class NativePreviewRoutes:
    def __init__(self, context):
        self.context = context

    async def _handle_stream(self, request):
        try:
            local = ipaddress.ip_address(request.remote).is_loopback
        except (ValueError, TypeError):
            local = False
        if not local:
            raise web.HTTPForbidden(text="Native preview requires a local connection")
        hub = getattr(self.context.node, "native_preview", None)
        name = request.query.get("camera", "")
        if hub is None or name not in hub.specs:
            raise web.HTTPNotFound(text="Unknown native camera")
        try:
            fps = max(5, min(30, int(request.query.get("fps", 30))))
        except ValueError:
            fps = 30
        ws = web.WebSocketResponse(heartbeat=20, compress=False)
        await ws.prepare(request)
        client = hub.connect(name, fps, asyncio.get_running_loop())
        self.context.node.viewer_connected()

        async def send():
            while not ws.closed:
                await client.event.wait()
                client.event.clear()
                for packet in client.packets():
                    # A stalled local viewer must not retain a growing TCP backlog.
                    await asyncio.wait_for(ws.send_bytes(packet), timeout=0.15)

        sender = asyncio.create_task(send())
        reader = asyncio.create_task(self._read(ws))
        try:
            await asyncio.wait((sender, reader), return_when=asyncio.FIRST_COMPLETED)
        finally:
            hub.disconnect(client)
            self.context.node.viewer_disconnected()
            for task in (sender, reader):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, ConnectionError, TimeoutError, ValueError):
                    await task
            await ws.close()
        return ws

    @staticmethod
    async def _read(ws):
        async for _message in ws:
            pass
