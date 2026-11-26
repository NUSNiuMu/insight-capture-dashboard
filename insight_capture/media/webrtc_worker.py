#!/usr/bin/env python3

"""Standalone WebRTC/GStreamer worker with authenticated AF_UNIX IPC."""

import argparse
import asyncio
import ctypes
import json
import multiprocessing
import os
import signal
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from insight_capture.media.preview_config import preview_description
from typing import Dict, Iterable, Optional

from aiohttp import web
from multiprocessing.connection import Listener

from insight_capture.core.config import build_dashboard_config, enabled_cameras, load_setup
from insight_capture.media.cdr import compressed_image_view
from insight_capture.media.h264 import H264Queue
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


class _H264Reader:
    """Keep ROS receive independent from preview encoder and capture load."""

    def __init__(self, cameras, playback, reliability, connection):
        import rclpy
        from rclpy.context import Context
        from rclpy.signals import SignalHandlerOptions
        from sensor_msgs.msg import CompressedImage

        from insight_capture.runtime.diagnostics import DiagnosticEvents
        from insight_capture.runtime.ros.node import make_image_qos
        from insight_capture.runtime.ros.topics import playback_topic
        from insight_capture.runtime.ros_inputs import CameraInputs

        self.dimensions = {camera["name"]: (int(camera.get("width", 0)), int(camera.get("height", 0)))
                           for camera in cameras}
        self.queues = {name: H264Queue(max_age_sec=.25) for name in self.dimensions}
        self.demand = set()
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.connection = connection
        self.logger = rclpy.logging.get_logger("insight_webrtc_h264")
        self.groups = {}
        qos = make_image_qos(depth=30, reliability=reliability)
        for camera in cameras:
            domain = camera["ros_domain_id"]
            if domain not in self.groups:
                context = Context()
                rclpy.init(context=context, domain_id=domain, signal_handler_options=SignalHandlerOptions.NO)
                node = rclpy.create_node(f"insight_webrtc_h264_{domain}", context=context, enable_rosout=False)
                node.events = DiagnosticEvents(node.get_logger())
                self.groups[domain] = context, node, CameraInputs(node)
            _, _, inputs = self.groups[domain]
            topic = playback_topic(camera["topic"]) if playback else camera["topic"]
            inputs.create_subscription(CompressedImage, topic, self._callback(camera["name"]), qos, raw=True)
        self.thread = threading.Thread(target=self._send, name="webrtc_h264_frames", daemon=True)
        self.thread.start()
        for _, _, inputs in self.groups.values():
            inputs.start()

    def set_demand(self, name, fps):
        if name not in self.queues:
            return
        with self.lock:
            if fps:
                self.demand.add(name)
            elif name in self.demand:
                self.demand.remove(name)
                self.queues[name].reset()

    def _callback(self, name):
        def receive(raw):
            with self.lock:
                if name not in self.demand:
                    return
                try:
                    image = compressed_image_view(raw)
                except (ValueError, IndexError) as exc:
                    self.logger.warning(f"Invalid preview H.264 image: {exc}", throttle_duration_sec=5)
                    return
                if image.format.lower() != "h264":
                    return
                stamp = image.header.stamp
                if self.queues[name].push(bytes(image.data), stamp.sec * 1_000_000_000 + stamp.nanosec):
                    self.ready.set()
        return receive

    def _send(self):
        report_at = time.monotonic()
        try:
            while not self.stopped.is_set():
                self.ready.wait(.25)
                self.ready.clear()
                for name, queue in self.queues.items():
                    width, height = self.dimensions[name]
                    for data, stamp, _ in queue.drain_with_metadata():
                        self.connection.send(("frame", (name, "H264", width, height, data, stamp)))
                now = time.monotonic()
                if now >= report_at:
                    self.connection.send(("stats", {name: queue.stats() for name, queue in self.queues.items()}))
                    report_at = now + 1
        except (EOFError, OSError):
            self.stopped.set()

    def close(self):
        for context, node, inputs in self.groups.values():
            inputs.close()
            node.destroy_node()
            context.try_shutdown()
        self.stopped.set()
        self.ready.set()
        self.thread.join()


def _read_h264(cameras, playback, reliability, connection, parent):
    _die_with_parent()
    if os.getppid() != parent:
        return
    reader = _H264Reader(cameras, playback, reliability, connection)
    try:
        while (command := connection.recv()) is not None:
            reader.set_demand(*command)
    except (EOFError, OSError):
        pass
    finally:
        reader.close()
        connection.close()


class H264PreviewInput:
    """Pass bounded H.264 queues across a dedicated process boundary."""

    def __init__(self, cameras, playback, reliability, deliver):
        ctx = multiprocessing.get_context("spawn")
        self.connection, output = ctx.Pipe()
        self.process = ctx.Process(
            target=_read_h264,
            args=(cameras, playback, reliability, output, os.getpid()),
            name="preview_h264_input",
            daemon=True,
        )
        self.stats = {}
        self.sources = {camera["name"] for camera in cameras}
        self.demand = set()
        self.closing = threading.Event()
        self.deliver = deliver
        self.process.start()
        output.close()
        self.thread = threading.Thread(target=self._receive, name="webrtc_h264_frames", daemon=True)
        self.thread.start()

    def set_demand(self, name, fps):
        expected = fps > 0
        if name not in self.sources or expected == (name in self.demand):
            return
        if expected:
            self.demand.add(name)
        else:
            self.demand.discard(name)
        try:
            self.connection.send((name, fps))
        except (EOFError, OSError):
            pass  # The reader thread handles an unexpected child exit.

    def _receive(self):
        try:
            while True:
                kind, payload = self.connection.recv()
                if kind == "stats":
                    self.stats = payload
                elif payload[0] in self.demand:
                    self.deliver(*payload)
        except (EOFError, OSError):
            if not self.closing.is_set():
                _log("H264 input process disconnected; restarting the preview worker")
                os.kill(os.getpid(), signal.SIGTERM)

    def close(self):
        self.closing.set()
        try:
            self.connection.send(None)
        except (EOFError, OSError):
            pass
        self.process.join(timeout=1)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=1)
        self.thread.join(timeout=1)
        self.connection.close()


def build_app(streams, ipc_server, h264=None):
    async def health(request):
        return web.json_response({"webrtc_available": ipc_server.ready(),
                                  "cameras": streams.snapshot_stats(),
                                  "h264_inputs": h264.stats if h264 else {}})
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
    parser.add_argument("--ros-domain-id", required=True, type=int)
    parser.add_argument("--playback", action="store_true")
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
    domains = {camera["name"]: int(camera.get("ros_domain_id", args.ros_domain_id))
               for camera in enabled_cameras(raw_config)}
    h264_cameras = [dict(camera, ros_domain_id=args.ros_domain_id if args.playback else domains[camera["device_name"]])
                    for camera in config["cameras"] if camera["type"] == "h264"]
    software_jpeg_sources = frozenset(c["name"] for c in config["cameras"] if c.get("uq2168"))
    streams = WebRtcStreams(description, args.webrtc_port, ipc_server.on_session_state_change, log=_log,
                            passthrough=[camera["name"] for camera in h264_cameras],
                            software_jpeg_sources=software_jpeg_sources)
    h264 = None
    if h264_cameras:
        reliability = config["trajectory"].get("image_qos_reliability", "best_effort")
        h264 = H264PreviewInput(h264_cameras, args.playback, reliability, streams.push_resolved_frame)

        def session_changed(name, fps):
            h264.set_demand(name, fps)
            ipc_server.on_session_state_change(name, fps)

        streams.changed = session_changed
    ipc_server.start(streams)

    try:
        app = build_app(streams, ipc_server, h264)
        web.run_app(app, host=args.host, port=args.webrtc_port, print=None)
    finally:
        if h264 is not None:
            h264.close()


if __name__ == "__main__":
    main()
