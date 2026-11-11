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
from types import SimpleNamespace
from insight_capture.media.preview_config import preview_description
from typing import Dict, Iterable, Optional

from aiohttp import web
from multiprocessing.connection import Listener

from insight_capture.core.config import build_dashboard_config, load_setup
from insight_capture.media.webrtc import WebRtcStreams

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
        self._session_state: Dict[str, int] = {name: 0 for name in camera_names}

    def start(self, streams: Optional[WebRtcStreams]) -> None:
        self._streams = streams
        threading.Thread(target=self._accept_loop, daemon=True, name="webrtc_ipc_accept").start()

    def ready(self) -> bool:
        with self._lock:
            return self._conn is not None

    def on_session_state_change(self, camera_name: str, target_fps: int) -> None:
        self._session_state[camera_name] = int(target_fps)
        with self._lock:
            conn = self._conn
            if conn is None:
                return
            try:
                conn.send(("session_state", camera_name, int(target_fps)))
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
                for name, target_fps in list(self._session_state.items()):
                    conn.send(("session_state", name, target_fps))
                while True:
                    message = conn.recv()
                    if not (isinstance(message, tuple) and len(message) == 6):
                        continue
                    camera_name, fmt, width, height, data, stamp = message
                    if self._streams is not None:
                        self._streams.push_resolved_frame(camera_name, fmt, width, height, data, stamp)
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


def build_app(streams, ipc_server):
    async def health(request):
        return web.json_response({"webrtc_available": ipc_server.ready(),
                                  "cameras": streams.snapshot_stats()})
    app = web.Application()
    app.router.add_get("/ws/webrtc", streams.websocket)
    app.router.add_get("/stream/{source}.h264", streams.h264)
    app.router.add_get("/healthz", health)
    app.on_startup.append(streams.start)
    app.on_shutdown.append(streams.close)
    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--host", default="127.0.0.1")
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
    description = preview_description([SimpleNamespace(**c) for c in config["cameras"]],
                                      [SimpleNamespace(**p) for p in config["poses"]])
    streams = WebRtcStreams(description, args.webrtc_port, ipc_server.on_session_state_change, log=_log)
    ipc_server.start(streams)

    app = build_app(streams, ipc_server)
    web.run_app(app, host=args.host, port=args.webrtc_port, print=None)


if __name__ == "__main__":
    main()
