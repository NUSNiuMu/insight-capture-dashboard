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
        """Queue a frame only when the camera has active WebRTC viewers."""
        if not self.owner._webrtc_has_sessions.get(camera_name):
            return
        # Wait for the matching composite instead of interleaving raw frames.
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
        # Preload JetPack multimedia dependencies before GStreamer starts.
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
        log_file.close()
        self.owner.get_logger().info(f"webrtc: spawned webrtc_worker.py pid={proc.pid} port={self.owner.webrtc_port}")
        return proc

    def stop_webrtc_worker(self) -> None:
        proc = getattr(self.owner, "_webrtc_proc", None)
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
        log_file.close()
        self.owner.get_logger().info(f"hand_overlay: spawned hand_overlay_worker.py pid={proc.pid}")
        return proc

    def ensure_hand_overlay_worker(self) -> None:
        with self.owner._hand_overlay_worker_lock:
            proc = getattr(self.owner, "_hand_overlay_proc", None)
            if proc is not None and proc.poll() is None:
                return
            self.owner._hand_overlay_proc = self._start_hand_overlay_worker()

    def stop_hand_overlay_worker(self) -> None:
        with self.owner._hand_overlay_worker_lock:
            proc = getattr(self.owner, "_hand_overlay_proc", None)
            if proc is None:
                return
            self.owner._hand_overlay_proc = None
            proc.terminate()
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3.0)
            self.owner._pending_hand_overlay_frames.clear()

    def _dispatch_hand_overlay(self, camera_name: str, version: int, jpeg_bytes: bytes, hands: list) -> None:
        """Keep only the newest overlay request per camera."""
        self.ensure_hand_overlay_worker()
        self.owner._pending_hand_overlay_frames[camera_name] = (version, jpeg_bytes, hands)
        self.owner._hand_overlay_frame_event.set()

    def _hand_overlay_ipc_loop(self) -> None:
        """Send overlay requests and apply results over one IPC connection."""
        authkey = self.owner._hand_overlay_authkey
        while rclpy is not None and rclpy.ok():
            if getattr(self.owner, "_hand_overlay_proc", None) is None:
                time.sleep(0.25)
                continue
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
                if getattr(self.owner, "_hand_overlay_proc", None) is not None:
                    self.owner.get_logger().warning(
                        f"hand overlay ipc: lost connection to worker ({exc}); reconnecting"
                    )
            finally:
                with contextlib.suppress(Exception):
                    conn.close()
            time.sleep(1.0)

    def _apply_composited_hand_overlay(self, camera_name: str, version: int, composited: bytes) -> None:
        """Apply a composite unless a newer result already won the race."""
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
        """Send frames and receive session state over one IPC connection."""
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
        """Poll worker health; any failure means WebRTC is unavailable."""
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
