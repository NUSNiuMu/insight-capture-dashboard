"""WebRTC and hand-overlay worker processes, IPC, and health."""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import subprocess
import sys
import time
from multiprocessing.connection import Client
from pathlib import Path

try:
    import rclpy
except Exception:  # pragma: no cover - fake mode can run without ROS imports
    rclpy = None

from hw_jpeg import HwJpegCodec

from .models import CameraFrame


class WorkerSupervisor:
    def __init__(self, owner) -> None:
        self.owner = owner

    def _maybe_queue_webrtc_frame(self, camera_name: str, topic_type: str, msg, frame) -> None:
        """Hand a frame to the webrtc_worker process's send queue, unless
        nobody's watching that camera over WebRTC right now.

        This check has to happen here, before resolving/copying any bytes --
        moving it to the IPC send thread instead would mean every frame
        still pays for the resolution work below even with zero viewers,
        exactly the per-frame cost push_ros_frame() used to skip in-process
        (see webrtc_stream.py's push_resolved_frame docstring). Gating state
        comes from webrtc_worker.py's create_session/close_session
        transitions, relayed over IPC into self.owner._webrtc_has_sessions (see
        _webrtc_ipc_loop).
        """
        if not self.owner._webrtc_has_sessions.get(camera_name):
            return
        # A hand-overlay worker has the current JPEG. Sending its raw source
        # now would interleave undecorated video frames with the late
        # composite; wait for _apply_composited_hand_overlay to forward the
        # matching JPEG instead.
        if frame is not None and frame.hand_overlay_pending:
            return
        if topic_type == "compressed":
            if frame is None or frame.width <= 0 or frame.height <= 0:
                return
            data, fmt, width, height = frame.data, "JPEG", frame.width, frame.height
        else:
            layout = HwJpegCodec.ros_image_layout(msg)
            if layout is None:
                return
            fmt, width, height = layout
            data = bytes(msg.data)
        self.owner._pending_webrtc_frames[camera_name] = (fmt, width, height, data)
        self.owner._webrtc_frame_event.set()

    def _start_webrtc_worker(self) -> "subprocess.Popen":
        script_path = Path(__file__).resolve().parents[1] / "webrtc_worker.py"
        env = dict(os.environ)
        env["INSIGHT_WEBRTC_AUTHKEY"] = self.owner._webrtc_authkey.hex()
        # JetPack's nvv4l2 encoder loads libnvmmlite_video at runtime. Its
        # symbols (NvOsSleepMS and video_parser_flush) are supplied by
        # sibling libraries but are not promoted to global scope reliably in
        # this container, so the worker can die only after H.264 pipelines
        # start. Preload the complete linked trio from the host-mounted
        # NVIDIA directory before importing GStreamer.
        nvidia_library_dir = Path("/usr/lib/aarch64-linux-gnu/nvidia")
        multimedia_libraries = (
            nvidia_library_dir / "libnvos.so",
            nvidia_library_dir / "libnvvideo.so",
            nvidia_library_dir / "libnvparser.so",
        )
        preload = [str(path) for path in multimedia_libraries if path.exists()]
        if preload:
            env["LD_PRELOAD"] = " ".join(preload + [env.get("LD_PRELOAD", "")]).strip()
        log_path = self.owner.project_root / "outputs" / "webrtc_worker.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "a", buffering=1)
        proc = subprocess.Popen(
            [
                sys.executable,
                str(script_path),
                "--config",
                str(self.owner.config_path),
                "--webrtc-port",
                str(self.owner.webrtc_port),
                "--ipc-socket",
                self.owner._webrtc_ipc_path,
            ],
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        self.owner.get_logger().info(f"webrtc: spawned webrtc_worker.py pid={proc.pid} port={self.owner.webrtc_port}")
        return proc

    def stop_webrtc_worker(self) -> None:
        proc = getattr(self, "_webrtc_proc", None)
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3.0)

    def _start_hand_overlay_worker(self) -> "subprocess.Popen":
        script_path = Path(__file__).resolve().parents[1] / "hand_overlay_worker.py"
        env = dict(os.environ)
        env["INSIGHT_HANDOVERLAY_AUTHKEY"] = self.owner._hand_overlay_authkey.hex()
        log_path = self.owner.project_root / "outputs" / "hand_overlay_worker.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "a", buffering=1)
        proc = subprocess.Popen(
            [
                sys.executable,
                str(script_path),
                "--ipc-socket",
                self.owner._hand_overlay_ipc_path,
            ],
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        self.owner.get_logger().info(f"hand_overlay: spawned hand_overlay_worker.py pid={proc.pid}")
        return proc

    def stop_hand_overlay_worker(self) -> None:
        proc = getattr(self, "_hand_overlay_proc", None)
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3.0)

    def _dispatch_hand_overlay(self, camera_name: str, version: int, jpeg_bytes: bytes, hands: list) -> None:
        """Fire-and-forget handoff to hand_overlay_worker.py -- called from
        hand_overlay.compose_hand_overlay_jpeg once it's decided this frame
        is worth overlaying. Only the newest dispatch per camera is kept
        (mirrors _pending_webrtc_frames), so a slow worker never backs up
        the camera's own frame worker thread."""
        self.owner._pending_hand_overlay_frames[camera_name] = (version, jpeg_bytes, hands)
        self.owner._hand_overlay_frame_event.set()

    def _hand_overlay_ipc_loop(self) -> None:
        """Owns the single connection to hand_overlay_worker.py: sends
        dispatched frames and applies composited results back into
        latest_camera_frames. Single thread on this side too (mirrors
        IpcServer in hand_overlay_worker.py) so no lock is needed around
        the Connection itself.owner."""
        authkey = self.owner._hand_overlay_authkey
        while rclpy is not None and rclpy.ok():
            try:
                conn = Client(self.owner._hand_overlay_ipc_path, family="AF_UNIX", authkey=authkey)
            except OSError:
                time.sleep(1.0)
                continue
            try:
                while rclpy is not None and rclpy.ok():
                    while conn.poll(0):
                        message = conn.recv()
                        if not (isinstance(message, tuple) and len(message) == 3):
                            continue
                        camera_name, version, composited = message
                        self.owner._apply_composited_hand_overlay(camera_name, version, composited)
                    if self.owner._hand_overlay_frame_event.wait(timeout=0.05):
                        self.owner._hand_overlay_frame_event.clear()
                        for camera_name in list(self.owner._pending_hand_overlay_frames.keys()):
                            payload = self.owner._pending_hand_overlay_frames.pop(camera_name, None)
                            if payload is None:
                                continue
                            conn.send((camera_name,) + payload)
            except (EOFError, OSError) as exc:
                self.owner.get_logger().warning(f"hand overlay ipc: lost connection to worker ({exc}); reconnecting")
            finally:
                with contextlib.suppress(Exception):
                    conn.close()
            time.sleep(1.0)

    def _apply_composited_hand_overlay(self, camera_name: str, version: int, composited: bytes) -> None:
        """Patches a worker's composited JPEG into latest_camera_frames.

        The round trip through hand_overlay_worker.py (two process hops
        plus a GStreamer hardware encode/decode) routinely takes longer
        than one camera frame period, so by the time a result comes back
        the raw frame it was dispatched from is almost never still the
        "current" one anymore -- requiring an exact version match (the
        first cut of this) silently discarded essentially every composite,
        which looked exactly like "hand overlay draws nothing" even though
        detection and compositing were both working. This only guards
        against applying a composite older than one already applied (so an
        out-of-order arrival can't flicker backwards); the served frame's
        own stamp_ns/version are left as whatever they already are --
        only the pixel bytes change, arriving a frame or two late is
        invisible at 20-30fps.
        """
        if version <= self.owner._hand_overlay_last_applied.get(camera_name, -1):
            return
        width, height = self.owner._jpeg_dimensions(composited)
        with self.owner.camera_frame_lock:
            current = self.owner.latest_camera_frames.get(camera_name)
            if current is None:
                return
            self.owner._hand_overlay_last_applied[camera_name] = version
            self.owner.latest_camera_frames[camera_name] = CameraFrame(
                data=composited,
                stamp_ns=current.stamp_ns,
                received_monotonic=time.monotonic(),
                mime_type="image/jpeg",
                width=width,
                height=height,
                version=version,
            )
        if self.owner._webrtc_has_sessions.get(camera_name):
            self.owner._pending_webrtc_frames[camera_name] = ("JPEG", width, height, composited)
            self.owner._webrtc_frame_event.set()

    def _webrtc_ipc_loop(self) -> None:
        """Owns the single connection to webrtc_worker.py: sends frames the
        worker actually has viewers for, and relays its session_state
        updates back into self.owner._webrtc_has_sessions. Single thread on this
        side too (mirrors IpcServer in webrtc_worker.py) so no lock is
        needed around the Connection itself.owner."""
        authkey = self.owner._webrtc_authkey
        while rclpy is not None and rclpy.ok():
            try:
                conn = Client(self.owner._webrtc_ipc_path, family="AF_UNIX", authkey=authkey)
            except OSError:
                time.sleep(1.0)
                continue
            try:
                while rclpy is not None and rclpy.ok():
                    while conn.poll(0):
                        message = conn.recv()
                        if isinstance(message, tuple) and len(message) == 3 and message[0] == "session_state":
                            _, camera_name, has_sessions = message
                            self.owner._webrtc_has_sessions[camera_name] = bool(has_sessions)
                    if self.owner._webrtc_frame_event.wait(timeout=0.05):
                        self.owner._webrtc_frame_event.clear()
                        for camera_name in list(self.owner._pending_webrtc_frames.keys()):
                            payload = self.owner._pending_webrtc_frames.pop(camera_name, None)
                            if payload is None:
                                continue
                            conn.send((camera_name,) + payload)
            except (EOFError, OSError) as exc:
                self.owner.get_logger().warning(f"webrtc ipc: lost connection to worker ({exc}); reconnecting")
            finally:
                with contextlib.suppress(Exception):
                    conn.close()
            time.sleep(1.0)

    def _webrtc_healthz_loop(self) -> None:
        """Polls webrtc_worker.py's own /healthz every 5s -- this is the
        replacement for the old in-process `self.owner.webrtc_streams is not
        None` check in build_camera_payload. Deliberately dumb (blocking
        stdlib http.client, short timeout, wide try/except): the worker not
        being up yet/having crashed/lacking hardware elements must all just
        read as unavailable here, same as today's fallback-to-polling
        behavior, not raise."""
        while rclpy is not None and rclpy.ok():
            available = False
            try:
                conn = http.client.HTTPConnection("127.0.0.1", self.owner.webrtc_port, timeout=2.0)
                try:
                    conn.request("GET", "/healthz")
                    resp = conn.getresponse()
                    payload = json.loads(resp.read())
                    available = bool(payload.get("webrtc_available"))
                finally:
                    conn.close()
            except Exception:
                available = False
            self.owner._webrtc_available_cached = available
            time.sleep(5.0)
