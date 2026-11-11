"""Same-origin phone signalling, gated before any worker or encoder starts."""
import asyncio
from contextlib import suppress
from aiohttp import ClientSession, ClientTimeout, WSMsgType, web


class PreviewRoutes:
    def __init__(self, context):
        self.context = context

    async def websocket(self, request):
        origin = request.headers.get("Origin")
        if origin and origin != f"{request.scheme}://{request.host}":
            raise web.HTTPForbidden(text="Cross-origin preview is not allowed")
        node = self.context.node
        gate = node._preview_manager
        if gate.paused:
            raise web.HTTPConflict(text="Preview paused during capture")
        await asyncio.to_thread(gate.activity)
        timeout = ClientTimeout(total=None, sock_connect=1)
        async with ClientSession(timeout=timeout) as session:
            for _ in range(50):
                if gate.paused:
                    raise web.HTTPConflict(text="Preview paused during capture")
                try:
                    async with session.get(f"http://127.0.0.1:{node.webrtc_port}/healthz") as response:
                        if response.status == 200 and (await response.json()).get("webrtc_available"):
                            break
                except (OSError, asyncio.TimeoutError):
                    pass
                await asyncio.sleep(.1)
            else:
                raise web.HTTPServiceUnavailable(text="Preview worker did not become ready")
            if gate.paused:
                raise web.HTTPConflict(text="Preview paused during capture")
            async with session.ws_connect(f"http://127.0.0.1:{node.webrtc_port}/ws/webrtc", params={"source": request.query.get("source", "__overview__")}) as upstream:
                ws = web.WebSocketResponse(heartbeat=15, max_msg_size=65536)
                await ws.prepare(request)
                async def relay(src, dst):
                    async for msg in src:
                        if gate.paused:
                            break
                        if msg.type == WSMsgType.TEXT:
                            await dst.send_str(msg.data)
                tasks = [asyncio.create_task(relay(ws, upstream)), asyncio.create_task(relay(upstream, ws))]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    with suppress(ConnectionError):
                        await ws.close()
                return ws
