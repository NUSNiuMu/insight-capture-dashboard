"""Offline go2rtc service and shared Jetson H264 producers."""
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from time import monotonic
from urllib.parse import quote

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web
from .h264 import nal_units
from .preview_config import FPS


NVMM_RETRY_SEC = 5


# A fresh interpreter avoids preexec_fn in this threaded worker. Bind the relay
# to its owner before exec so a killed worker cannot leave ports/processes behind.
_RELAY_CHILD = """
import os, sys
from insight_capture.media.webrtc_worker import _die_with_parent
_die_with_parent()
if os.getppid() != int(sys.argv[1]):
    raise SystemExit(0)
os.execv(sys.argv[2], sys.argv[2:])
"""


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
    def __init__(self, description, port, on_session_state_change, log=print, *, passthrough=(), software_jpeg_sources=()):
        self.description, self.port, self.log = description, port, log
        self.changed = on_session_state_change
        self.passthrough = set(passthrough)
        self.software_jpeg_sources = software_jpeg_sources
        self.sources = {s["id"]: {**s, "regions": [dict(id=s["id"], x=0, y=0, width=s["width"], height=s["height"])]}
                        for s in description["sources"]}
        if description["overview"]:
            self.sources["__overview__"] = description["overview"]
        self.lock = threading.Lock()
        self.producers = {}
        self._nvmm_retry_at = 0.0
        self.watchers = {}
        self.clients = set()
        self.services = {}
        self.listeners = {}
        self.service_lock = asyncio.Lock()
        self.producer_lock = asyncio.Lock()
        self.session = None
        self.loop = None
        self.media_port = port + 2

    async def start(self, app):
        self.loop = asyncio.get_running_loop()
        self.session = ClientSession(timeout=ClientTimeout(total=None, sock_connect=3))

    async def service(self, address):
        # ICE learns peer-reflexive addresses too, so signalling filters alone
        # cannot isolate AP/USB/office networks. Bind one lightweight relay per
        # page-facing address; their consumers share the same H264 encoder.
        async with self.service_lock:
            existing = self.services.get(address)
            if existing and existing[0].poll() is None:
                return existing[2]
            if existing:
                existing[1].cleanup()
                del self.services[address]
            return await self._start_service(address)

    async def _start_service(self, address):
        binary = Path(__file__).resolve().parents[2] / "deploy/bin/go2rtc"
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise RuntimeError("Bundled go2rtc is missing or not executable")
        addresses = json.loads(subprocess.check_output(["ip", "-j", "-4", "addr"], text=True))
        hosts = {a["local"] for iface in addresses for a in iface.get("addr_info", [])}
        if address not in hosts:
            raise web.HTTPBadRequest(text="Preview address is not on this device")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            api_port = probe.getsockname()[1]
        config = {"api": {"listen": f"127.0.0.1:{api_port}", "allow_paths": ["/api/ws"]},
                  "rtsp": {"listen": ""}, "srt": {"listen": ""},
                  "webrtc": {"listen": f"{address}:{self.media_port}",
                             "ice_servers": [],
                             "candidates": [f"{address}:{self.media_port}"],
                             "filters": {"ips": [address], "loopback": address.startswith("127."),
                                         "networks": ["udp4", "tcp4"]}},
                  "streams": {name: f"http://127.0.0.1:{self.port}/stream/{quote(name, safe='')}.h264" for name in self.sources},
                  "log": {"level": "warn"}}
        temp = tempfile.TemporaryDirectory(prefix="insight-preview-")
        path = Path(temp.name) / "go2rtc.json"
        path.write_text(json.dumps(config))
        process = subprocess.Popen([sys.executable, "-c", _RELAY_CHILD, str(os.getpid()),
                                    str(binary), "-config", str(path)])
        self.services[address] = (process, temp, api_port)
        try:
            for _ in range(60):
                if process.poll() is not None:
                    raise RuntimeError("go2rtc exited during startup")
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", api_port)
                    writer.close()
                    await writer.wait_closed()
                    return api_port
                except OSError:
                    await asyncio.sleep(.05)
            raise RuntimeError("go2rtc did not start")
        except BaseException:
            await self._stop_service(process, temp)
            self.services.pop(address, None)
            raise

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
        api_port = await self.service(request.query.get("address", "127.0.0.1"))
        ws = web.WebSocketResponse(heartbeat=15, max_msg_size=65536)
        await ws.prepare(request)
        self.clients.add(ws)
        self.watchers[source] = self.watchers.get(source, 0) + 1
        self._update_demand()
        try:
            async with self.session.ws_connect(f"http://127.0.0.1:{api_port}/api/ws?src={quote(source, safe='')}") as upstream:
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

    def _publish_h264(self, name, data, keyframe):
        def enqueue():
            for pending, listener in self.listeners.get(name, {}).items():
                if listener["overflow"]:
                    continue
                if not listener["started"]:
                    if not keyframe:
                        continue
                    listener["started"] = True
                if pending.full():
                    # Reconnect this viewer at a new GOP.
                    listener["overflow"] = True
                else:
                    pending.put_nowait(data)
        self.loop.call_soon_threadsafe(enqueue)

    def _new_encoder(self, name, deliver):
        from .jetson_preview import OverviewEncoder

        description = self.sources[name]
        regions = description["regions"]
        if (name == "__overview__" and len(regions) == 4 and monotonic() >= self._nvmm_retry_at
                and all(region["id"] in self.passthrough for region in regions)):
            try:
                from .nvmm_preview import NvmmOverviewEncoder

                return NvmmOverviewEncoder(description, deliver, self.log)
            except Exception as exc:
                self._fallback_nvmm(exc, retry=False)
        return OverviewEncoder(description, deliver, self.log, software_jpeg_sources=self.software_jpeg_sources)

    def _fallback_nvmm(self, reason, *, retry=True):
        now = monotonic()
        if now >= self._nvmm_retry_at:
            self._nvmm_retry_at = now + NVMM_RETRY_SEC if retry else float("inf")
            retry_message = f"; retry on a new encoder after {NVMM_RETRY_SEC}s" if retry else ""
            self.log(f"NVMM overview unavailable; using the regular encoder: {reason}{retry_message}")

    async def h264(self, request):
        # This endpoint is bound to loopback with the worker and only go2rtc uses it.
        name = request.match_info["source"]
        if name not in self.sources or self.watchers.get(name, 0) == 0:
            raise web.HTTPNotFound()
        queue = asyncio.Queue(maxsize=16)
        state = {"overflow": False, "started": False}
        async with self.producer_lock:
            self.listeners.setdefault(name, {})[queue] = state
            if name not in self.passthrough and name not in self.producers:
                def deliver(data, keyframe):
                    self._publish_h264(name, data, keyframe)

                try:
                    producer = await asyncio.to_thread(self._new_encoder, name, deliver)
                except BaseException:
                    self.listeners[name].pop(queue, None)
                    raise
                with self.lock:
                    self.producers[name] = producer
            with self.lock:
                producer = self.producers.get(name)
        native = getattr(producer, "native_overview", False)
        response = web.StreamResponse(headers={"Content-Type": "video/h264", "Cache-Control": "no-store"})
        try:
            await response.prepare(request)
            timeout = 6 if native else 3 if name in self.passthrough else 5
            while self.watchers.get(name, 0) and not state["overflow"]:
                if native and producer.error is not None:
                    self._fallback_nvmm(producer.error)
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    if native and self.watchers.get(name, 0) and self.producers.get(name) is producer:
                        self._fallback_nvmm(f"no encoded frames for {timeout}s")
                    break
                await asyncio.wait_for(response.write(data), timeout=2)
                timeout = 3
        except (ConnectionError, asyncio.TimeoutError):
            pass
        finally:
            async with self.producer_lock:
                self.listeners[name].pop(queue, None)
                if not self.listeners[name]:
                    with self.lock:
                        producer = self.producers.pop(name, None)
                    if producer:
                        await asyncio.to_thread(producer.close)
        return response

    def push_resolved_frame(self, name, fmt, width, height, data, stamp):
        if fmt == "H264" and name in self.passthrough and self.watchers.get(name, 0):
            self._publish_h264(name, data, any(kind == 5 for kind, _ in nal_units(data)))
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
        for process, temp, _port in self.services.values():
            await self._stop_service(process, temp)
        self.services.clear()

    @staticmethod
    async def _stop_service(process, temp):
        if process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, 2)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait)
        temp.cleanup()
