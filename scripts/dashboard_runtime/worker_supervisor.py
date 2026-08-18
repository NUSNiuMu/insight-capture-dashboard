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

from dashboard_media.jpeg import HwJpegCodec
from dashboard_media.rate import select_frame

from .models import CameraFrame


class WorkerSupervisor:
    def __init__(self, owner) -> None:
        self.owner = owner

    def _queue_webrtc_frame(
        self,
        camera_name: str,
        payload: tuple[str, int, int, bytes],
        *,
        rate_checked: bool = False,
    ) -> None:
        """Publish the newest frame and count overwrites before IPC delivery."""
        if not rate_checked and not self._webrtc_frame_due(camera_name):
            return
        with self.owner._webrtc_metrics_lock:
            metrics = self.owner._webrtc_main_metrics[camera_name]
            metrics["queued"] += 1
            if camera_name in self.owner._pending_webrtc_frames:
                metrics["replaced"] += 1
            self.owner._pending_webrtc_frames[camera_name] = payload
        self.owner._webrtc_frame_event.set()

    def _webrtc_frame_due(self, camera_name: str) -> bool:
        """Select a stable preview cadence before copying a frame into IPC."""
        now = time.monotonic()
        with self.owner._webrtc_metrics_lock:
            target_fps = int(self.owner._webrtc_session_fps.get(camera_name, 0))
            if target_fps <= 0:
                return False
            next_at = self.owner._next_webrtc_frame_at.get(camera_name, 0.0)
            selected, next_at = select_frame(now, next_at, target_fps)
            if not selected:
                self.owner._webrtc_main_metrics[camera_name]["throttled"] += 1
                return False
            self.owner._next_webrtc_frame_at[camera_name] = next_at
            return True

    def _maybe_queue_webrtc_frame(self, camera_name: str, topic_type: str, msg, frame) -> None:
        """Queue a frame only when the camera has active WebRTC viewers."""
        if not self.owner._webrtc_has_sessions.get(camera_name):
            return
        # Wait for the matching composite instead of interleaving raw frames.
        if frame is not None and frame.hand_overlay_pending:
            return
        if not self._webrtc_frame_due(camera_name):
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
        self._queue_webrtc_frame(
            camera_name, (fmt, width, height, data), rate_checked=True
        )

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

    def ensure_webrtc_worker(self) -> None:
        """Start WebRTC only after the first viewer lease is acquired."""
        with self.owner._webrtc_worker_lock:
            proc = getattr(self.owner, "_webrtc_proc", None)
            if proc is not None and proc.poll() is None:
                return
            self.owner._webrtc_proc = self._start_webrtc_worker()

    def stop_webrtc_worker(self) -> None:
        with self.owner._webrtc_worker_lock:
            proc = getattr(self.owner, "_webrtc_proc", None)
            if proc is None:
                return
            self.owner._webrtc_proc = None
            proc.terminate()
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3.0)
            self.owner._webrtc_available_cached = False
            self.owner._webrtc_has_sessions.clear()
            self.owner._webrtc_session_fps.clear()
            self.owner._pending_webrtc_frames.clear()

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
            self._queue_webrtc_frame(camera_name, ("JPEG", width, height, composited))

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
                            _, camera_name, target_fps = message
                            target_fps = max(0, int(target_fps))
                            self.owner._webrtc_session_fps[camera_name] = target_fps
                            self.owner._webrtc_has_sessions[camera_name] = target_fps > 0
                            self.owner._next_webrtc_frame_at[camera_name] = 0.0
                    if self.owner._webrtc_frame_event.wait(timeout=0.05):
                        self.owner._webrtc_frame_event.clear()
                        for camera_name in list(self.owner._pending_webrtc_frames.keys()):
                            with self.owner._webrtc_metrics_lock:
                                payload = self.owner._pending_webrtc_frames.pop(
                                    camera_name, None
                                )
                            if payload is None:
                                continue
                            conn.send((camera_name,) + payload)
                            with self.owner._webrtc_metrics_lock:
                                self.owner._webrtc_main_metrics[camera_name]["ipc_sent"] += 1
            except (EOFError, OSError) as exc:
                self.owner.get_logger().warning(f"webrtc ipc: lost connection to worker ({exc}); reconnecting")
            finally:
                with contextlib.suppress(Exception):
                    conn.close()
            time.sleep(1.0)

    def _webrtc_healthz_loop(self) -> None:
        """Poll worker health; any failure means WebRTC is unavailable."""
        previous_at = time.monotonic()
        previous_main = {}
        previous_worker = {}
        while rclpy is not None and rclpy.ok():
            available = False
            worker_stats = {}
            proc = getattr(self.owner, "_webrtc_proc", None)
            if proc is None or proc.poll() is not None:
                self.owner._webrtc_available_cached = False
                time.sleep(1.0)
                continue
            try:
                conn = http.client.HTTPConnection("127.0.0.1", self.owner.webrtc_port, timeout=2.0)
                try:
                    conn.request("GET", "/healthz")
                    resp = conn.getresponse()
                    payload = json.loads(resp.read())
                    available = bool(payload.get("webrtc_available"))
                    raw_stats = payload.get("cameras", {})
                    if isinstance(raw_stats, dict):
                        worker_stats = raw_stats
                finally:
                    conn.close()
            except Exception:
                available = False
            now = time.monotonic()
            elapsed = max(now - previous_at, 1e-6)
            self.owner._webrtc_available_cached = available
            with self.owner._webrtc_metrics_lock:
                for camera_name, metrics in self.owner._webrtc_main_metrics.items():
                    prior = previous_main.get(camera_name, {})
                    for total_key, rate_key in (
                        ("queued", "queued_fps"),
                        ("throttled", "throttled_fps"),
                        ("replaced", "replaced_fps"),
                        ("ipc_sent", "ipc_fps"),
                    ):
                        delta = int(metrics.get(total_key, 0)) - int(
                            prior.get(total_key, 0)
                        )
                        metrics[rate_key] = max(0, delta) / elapsed
                    previous_main[camera_name] = {
                        key: int(metrics.get(key, 0))
                        for key in ("queued", "throttled", "replaced", "ipc_sent")
                    }
                enriched_worker_stats = {}
                for camera_name, metrics in worker_stats.items():
                    enriched = dict(metrics)
                    prior = previous_worker.get(camera_name, {})
                    for total_key, rate_key in (
                        ("worker_received", "worker_received_fps"),
                        ("appsrc_pushed", "appsrc_fps"),
                        ("encoded", "encoded_fps"),
                        ("throttled", "throttled_fps"),
                    ):
                        delta = int(enriched.get(total_key, 0)) - int(
                            prior.get(total_key, 0)
                        )
                        enriched[rate_key] = max(0, delta) / elapsed
                    enriched_worker_stats[camera_name] = enriched
                    previous_worker[camera_name] = {
                        key: int(enriched.get(key, 0))
                        for key in (
                            "worker_received",
                            "appsrc_pushed",
                            "encoded",
                            "throttled",
                        )
                    }
                self.owner._webrtc_worker_stats = enriched_worker_stats
            previous_at = now
            time.sleep(5.0)
