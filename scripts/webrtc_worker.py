#!/usr/bin/env python3

"""Standalone WebRTC/GStreamer worker with authenticated AF_UNIX IPC."""

import argparse
import asyncio
import ctypes
import json
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Dict, Iterable, Optional

from aiohttp import web
from multiprocessing.connection import Listener

from camera_setup import build_dashboard_config, load_setup
from webrtc_stream import WebRtcStreams

_AUTHKEY_ENV = "INSIGHT_WEBRTC_AUTHKEY"
_PR_SET_PDEATHSIG = 1


def _die_with_parent() -> None:
    """Use Linux PDEATHSIG to prevent an orphaned worker."""
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    except Exception:
        pass


class IpcServer:
    """Own the single reconnectable AF_UNIX connection to the main process."""

    def __init__(self, address: str, authkey: bytes, camera_names: Iterable[str], log) -> None:
        self._address = address
        self._authkey = authkey
        self._log = log
        self._lock = threading.Lock()
        self._conn = None
        self._streams: Optional[WebRtcStreams] = None
        self._session_state: Dict[str, bool] = {name: False for name in camera_names}

    def start(self, streams: Optional[WebRtcStreams]) -> None:
        self._streams = streams
        threading.Thread(target=self._accept_loop, daemon=True, name="webrtc_ipc_accept").start()

    def ready(self) -> bool:
        with self._lock:
            return self._conn is not None

    def on_session_state_change(self, camera_name: str, has_sessions: bool) -> None:
        self._session_state[camera_name] = has_sessions
        with self._lock:
            conn = self._conn
            if conn is None:
                return
            try:
                conn.send(("session_state", camera_name, has_sessions))
            except OSError:
                pass  # accept loop will notice on its next recv() and clean up

    def _accept_loop(self) -> None:
        try:
            if os.path.exists(self._address):
                os.unlink(self._address)
        except OSError:
            pass
        listener = Listener(address=self._address, family="AF_UNIX", authkey=self._authkey)
        self._log(f"webrtc ipc: listening on {self._address}")
        while True:
            conn = listener.accept()
            self._log("webrtc ipc: main process connected")
            with self._lock:
                self._conn = conn
            try:
                # Resend all session state after every connection.
                for name, has_sessions in list(self._session_state.items()):
                    conn.send(("session_state", name, has_sessions))
                while True:
                    message = conn.recv()
                    if not (isinstance(message, tuple) and len(message) == 5):
                        continue
                    camera_name, fmt, width, height, data = message
                    if self._streams is not None:
                        self._streams.push_resolved_frame(camera_name, fmt, width, height, data)
            except (EOFError, OSError) as exc:
                self._log(f"webrtc ipc: main process disconnected ({exc}); waiting for reconnect")
            finally:
                with self._lock:
                    self._conn = None
                try:
                    conn.close()
                except Exception:
                    pass


def _log(message: str) -> None:
    print(f"[webrtc_worker] {message}", flush=True)


def build_app(streams: Optional[WebRtcStreams], camera_topic_types: Dict[str, str], ipc_server: IpcServer) -> web.Application:
    async def handle_webrtc_ws(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20.0)
        await ws.prepare(request)
        camera_name = request.query.get("camera", "")
        topic_type = camera_topic_types.get(camera_name)
        if streams is None or topic_type is None:
            await ws.send_json({"type": "webrtc_unavailable"})
            await ws.close()
            return ws
        loop = asyncio.get_running_loop()

        def send_signal(payload: Dict) -> None:
            # Offer/ICE callbacks land on GStreamer threads; hop back onto
            # this process's own event loop for the websocket write.
            try:
                asyncio.run_coroutine_threadsafe(ws.send_json(payload), loop)
            except RuntimeError:
                pass  # loop shutting down; the session is about to close too

        try:
            target_fps = max(5, min(30, int(request.query.get("fps", "30"))))
        except (TypeError, ValueError):
            target_fps = 30
        session = streams.create_session(
            camera_name,
            send_signal,
            target_fps=target_fps,
        )
        try:
            async for message in ws:
                if message.type != web.WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(message.data)
                except json.JSONDecodeError:
                    continue
                kind = data.get("type")
                if kind == "answer":
                    session.set_remote_answer(str(data.get("sdp", "")))
                elif kind == "ice" and data.get("candidate"):
                    session.add_ice_candidate(
                        int(data.get("sdpMLineIndex") or 0), str(data["candidate"])
                    )
        finally:
            streams.close_session(session)
        return ws

    async def handle_healthz(_request: web.Request) -> web.Response:
        available = streams is not None and ipc_server.ready()
        return web.json_response({"webrtc_available": available})

    app = web.Application()
    app.router.add_get("/ws/webrtc", handle_webrtc_ws)
    app.router.add_get("/healthz", handle_healthz)
    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--webrtc-port", type=int, default=8766)
    parser.add_argument("--ipc-socket", required=True)
    return parser.parse_args()


def main() -> None:
    _die_with_parent()
    args = parse_args()

    authkey_hex = os.environ.get(_AUTHKEY_ENV)
    if not authkey_hex:
        print(f"webrtc_worker: missing {_AUTHKEY_ENV} env var; refusing to start", file=sys.stderr)
        sys.exit(1)
    # Decode the main process's hex-encoded IPC auth key.
    authkey = bytes.fromhex(authkey_hex)

    raw_config = load_setup(Path(args.config))
    config = build_dashboard_config(raw_config)
    camera_topic_types = {item["name"]: item["type"] for item in config.get("cameras", [])}

    ipc_server = IpcServer(args.ipc_socket, authkey, camera_topic_types.keys(), log=_log)
    streams = WebRtcStreams.create(log=_log, on_session_state_change=ipc_server.on_session_state_change)
    ipc_server.start(streams)

    app = build_app(streams, camera_topic_types, ipc_server)
    web.run_app(app, host=args.host, port=args.webrtc_port, print=None)


if __name__ == "__main__":
    main()
