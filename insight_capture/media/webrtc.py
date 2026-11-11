"""Offline go2rtc service and shared Jetson H264 producers."""
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from urllib.parse import quote

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web
from .preview_config import FPS


def validate_signal(raw, offered):
    """Independent copy of Ego's narrow go2rtc browser signalling contract."""
    if len(raw) > 65536:
        raise ValueError("预览信令过大")
    msg = json.loads(raw)
    if not isinstance(msg, dict) or not isinstance(msg.get("value"), str):
        raise ValueError("无效预览信令")
    kind, value = msg.get("type"), msg["value"]
    if kind == "webrtc/offer" and not offered:
        if " H264/90000" not in value or "a=recvonly" not in value:
            raise ValueError("客户端未提供 H264 WebRTC 接收能力")
        offered = True
    elif kind != "webrtc/candidate" or not offered or len(value) > 4096:
        raise ValueError("不支持的预览信令")
    return json.dumps({"type": kind, "value": value}), offered


class WebRtcStreams:
    def __init__(self, description, port, on_session_state_change, log=print):
        self.description, self.port, self.log = description, port, log
        self.changed = on_session_state_change
        self.sources = {s["id"]: {**s, "regions": [dict(id=s["id"], x=0, y=0, width=s["width"], height=s["height"])]}
                        for s in description["sources"]}
        if description["overview"]:
            self.sources["__overview__"] = description["overview"]
        self.lock = threading.Lock()
        self.producers = {}
        self.watchers = {}
        self.clients = set()
        self.process = None
        self.temp = None
        self.session = None
        self.loop = None
        self.api_port = port + 1
        self.media_port = port + 2

    async def start(self, app):
        self.loop = asyncio.get_running_loop()
        binary = Path(__file__).resolve().parents[2] / "deploy/bin/go2rtc"
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise RuntimeError("Bundled go2rtc is missing or not executable")
        # No STUN/TURN or cloud candidates. Only addresses actually on this Jetson.
        addresses = json.loads(subprocess.check_output(["ip", "-j", "-4", "addr"], text=True))
        hosts = [a["local"] for iface in addresses for a in iface.get("addr_info", [])
                 if a.get("scope") == "global" and not a["local"].startswith("169.254.")]
        config = {"api": {"listen": f"127.0.0.1:{self.api_port}", "allow_paths": ["/api/ws"]},
                  "rtsp": {"listen": ""}, "srt": {"listen": ""},
                  "webrtc": {"listen": f":{self.media_port}",
                             "ice_servers": [],
                             "candidates": [f"{h}:{self.media_port}" for h in hosts],
                             "filters": {"ips": hosts, "networks": ["udp4", "tcp4"]}},
                  "streams": {name: f"http://127.0.0.1:{self.port}/stream/{quote(name, safe='')}.h264" for name in self.sources},
                  "log": {"level": "warn"}}
        self.temp = tempfile.TemporaryDirectory(prefix="insight-preview-")
        path = Path(self.temp.name) / "go2rtc.json"
        path.write_text(json.dumps(config))
        self.process = subprocess.Popen([str(binary), "-config", str(path)])
        self.session = ClientSession(timeout=ClientTimeout(total=None, sock_connect=3))
        for _ in range(60):
            if self.process.poll() is not None:
                raise RuntimeError("go2rtc exited during startup")
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", self.api_port)
                writer.close()
                await writer.wait_closed()
                return
            except OSError:
                await asyncio.sleep(.05)
        raise RuntimeError("go2rtc did not start")

    def _update_demand(self):
        required = {region["id"] for source, count in self.watchers.items() if count
                    for region in self.sources[source]["regions"]}
        for source in self.description["sources"]:
            self.changed(source["id"], FPS if source["id"] in required else 0)

    async def websocket(self, request):
        source = request.query.get("source", "__overview__")
        if source not in self.sources:
            raise web.HTTPNotFound(text="Unknown preview source")
        if len(self.clients) >= 4:
            raise web.HTTPTooManyRequests(text="Too many preview viewers")
        ws = web.WebSocketResponse(heartbeat=15, max_msg_size=65536)
        await ws.prepare(request)
        self.clients.add(ws)
        self.watchers[source] = self.watchers.get(source, 0) + 1
        self._update_demand()
        try:
            async with self.session.ws_connect(f"http://127.0.0.1:{self.api_port}/api/ws?src={quote(source, safe='')}") as upstream:
                async def outbound():
                    offered = False
                    async for msg in ws:
                        if msg.type == WSMsgType.TEXT:
                            raw, offered = validate_signal(msg.data, offered)
                            await upstream.send_str(raw)
                async def inbound():
                    async for msg in upstream:
                        if msg.type == WSMsgType.TEXT:
                            await ws.send_str(msg.data)
                tasks = [asyncio.create_task(outbound()), asyncio.create_task(inbound())]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (ValueError, OSError, asyncio.TimeoutError) as exc:
            with suppress(Exception):
                await ws.send_json({"type": "error", "value": str(exc)})
        finally:
            self.clients.discard(ws)
            self.watchers[source] -= 1
            self._update_demand()
            if self.watchers[source] == 0:
                with self.lock:
                    producer = self.producers.pop(source, None)
                if producer:
                    await asyncio.to_thread(producer.close)
            await ws.close()
        return ws

    async def h264(self, request):
        # This endpoint is bound to loopback with the worker and only go2rtc uses it.
        name = request.match_info["source"]
        if name not in self.sources or self.watchers.get(name, 0) == 0:
            raise web.HTTPNotFound()
        from .jetson_preview import OverviewEncoder
        queue = asyncio.Queue(maxsize=16)
        closed = False
        overflow = False
        def deliver(data):
            def enqueue():
                nonlocal overflow
                if closed or overflow:
                    return
                if queue.full():
                    # Reconnect at a new GOP instead of sending a corrupted H264 tail.
                    overflow = True
                    return
                queue.put_nowait(data)
            self.loop.call_soon_threadsafe(enqueue)
        producer = await asyncio.to_thread(OverviewEncoder, self.sources[name], deliver, self.log)
        with self.lock:
            old = self.producers.get(name)
            self.producers[name] = producer
        if old:
            await asyncio.to_thread(old.close)
        response = web.StreamResponse(headers={"Content-Type": "video/h264", "Cache-Control": "no-store"})
        try:
            await response.prepare(request)
            while self.watchers.get(name, 0) and not overflow:
                data = await asyncio.wait_for(queue.get(), timeout=3)
                await asyncio.wait_for(response.write(data), timeout=2)
        except (ConnectionError, asyncio.TimeoutError):
            pass
        finally:
            closed = True
            with self.lock:
                if self.producers.get(name) is producer:
                    self.producers.pop(name, None)
            await asyncio.to_thread(producer.close)
        return response

    def push_resolved_frame(self, name, fmt, width, height, data, stamp):
        with self.lock:
            producers = list(self.producers.values())
        for producer in producers:
            producer.push(name, fmt, width, height, data, stamp)

    def snapshot_stats(self):
        with self.lock:
            return {name: producer.status() for name, producer in self.producers.items()}

    async def close(self, app):
        for client in list(self.clients):
            await client.close(code=1001, message=b"Preview paused")
        with self.lock:
            producers, self.producers = list(self.producers.values()), {}
        for producer in producers:
            await asyncio.to_thread(producer.close)
        if self.session:
            await self.session.close()
        if self.process:
            self.process.terminate()
            try:
                await asyncio.to_thread(self.process.wait, 2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                await asyncio.to_thread(self.process.wait)
        if self.temp:
            self.temp.cleanup()
