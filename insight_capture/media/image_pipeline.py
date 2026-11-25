"""Latest-frame preview routing and hardware JPEG encoding."""

from __future__ import annotations

import time

try:
    import rclpy
except Exception:  # pragma: no cover - host-side tools may import without ROS
    rclpy = None

from insight_capture.core.performance import track

from insight_capture.core.models import CameraFrame
from insight_capture.core.timing import source_clock_consistent
from insight_capture.media.cdr import compressed_image_view
from insight_capture.media.jpeg import jpeg_dimensions


class ImagePipeline:
    def __init__(self, owner) -> None:
        self.owner = owner
        self._encoding_paths = {}
        self._clock_samples = {}

    def clock_status(self):
        now = time.monotonic()
        with self.owner.camera_input_lock:
            samples = dict(self._clock_samples)
        devices = {}
        for camera in self.owner.cameras:
            sample = samples.get(camera.name)
            if sample is None:
                devices[camera.name] = {"ok": False, "state": "missing", "error": "No source timestamp"}
                continue
            stamp_ns, wall_ns, received = sample
            difference = (stamp_ns - wall_ns) / 1e9
            age = now - received
            # An old sample cannot diagnose the current clock. Lost input is
            # unavailable evidence, even if its last timestamp was incorrect.
            state = "stale" if age >= 2 else "ready" if source_clock_consistent(stamp_ns, wall_ns) else "invalid"
            devices[camera.name] = {"ok": state == "ready", "state": state,
                                    "source_minus_receipt_ms": round(difference * 1000, 3),
                                    "age_sec": round(age, 3)}
        return {"ok": bool(devices) and all(d["ok"] for d in devices.values()), "devices": devices,
                "method": "Image header versus Jetson receipt; detects wrong epochs, not NTP precision"}

    def encoding_status(self):
        """Report the last successful encoder, not hardware availability."""
        now = time.monotonic()
        return {name: {"last_output_path": path, "age_sec": now - stamp,
                       "recent": now - stamp <= 2.0}
                for name, (path, stamp) in self._encoding_paths.copy().items()}

    def _make_dashboard_image_callback(
        self, camera_name: str, topic_type: str, is_live: bool = True
    ):
        camera_topic = next(c.topic for c in self.owner.cameras if c.name == camera_name)
        event = self.owner._pending_frame_events[camera_name]

        # Keep callbacks near-zero cost so recording frames reach the queue.
        # Live and /bagplay sources share a slot but are gated by playback mode.
        def callback(msg) -> None:
            if is_live == self.owner._playback_mode:
                return
            if topic_type == "h264" and isinstance(msg, bytes):
                try:
                    msg = compressed_image_view(msg)
                except (ValueError, IndexError) as exc:
                    self.owner.get_logger().warning(f"Invalid H.264 image: {exc}", throttle_duration_sec=5)
                    return
            with self.owner.camera_input_lock:
                received = time.monotonic()
                self.owner.camera_input_times[camera_name].append(received)
                if is_live:
                    self._clock_samples[camera_name] = (int(msg.header.stamp.sec) * 1_000_000_000 + int(msg.header.stamp.nanosec), time.time_ns(), received)
            if not self.owner._playback_mode:
                self.owner._feed_recording_writer(camera_topic, msg)
            # Capture keeps the DDS reader and header audit without display work.
            if not self.owner.preview_requested():
                return
            native = getattr(self.owner, "native_preview", None)
            if native is not None:
                native.submit(camera_name, msg)
            if topic_type == "h264":
                self.owner._worker_supervisor.queue_h264(camera_name, msg)
                return
            self.owner._pending_frames[camera_name] = msg
            event.set()

        return callback

    def _frame_worker_loop(self, camera_name: str, topic_type: str) -> None:
        event = self.owner._pending_frame_events[camera_name]
        while rclpy is not None and rclpy.ok():
            if not event.wait(timeout=1.0):
                continue
            event.clear()
            msg = self.owner._pending_frames.pop(camera_name, None)
            if msg is None:
                continue
            if not self.owner.preview_requested():
                continue
            try:
                now = time.monotonic()
                native = getattr(self.owner, "native_preview", None)
                # Source JPEG also feeds WebRTC; raw streams use their active transport only.
                encode_jpeg = (
                    topic_type == "compressed"
                    or not (self.owner._webrtc_has_sessions.get(camera_name)
                            or (native is not None and native.active(camera_name)))
                )
                frame = (
                    self.owner._encode_dashboard_frame(camera_name, topic_type, msg)
                    if encode_jpeg
                    else None
                )
                with self.owner.camera_frame_lock:
                    self.owner.latest_camera_frames[camera_name] = frame
                    self.owner.camera_frame_times[camera_name].append(now)
                self.owner._maybe_queue_webrtc_frame(camera_name, topic_type, msg, frame)
            except Exception as exc:  # noqa: BLE001 - a bad frame must not kill the worker
                self.owner.get_logger().warning(f"frame worker {camera_name}: {exc}")

    def _encode_dashboard_frame(
        self,
        camera_name: str,
        topic_type: str,
        msg: object,
    ) -> CameraFrame:
        stamp_ns = self.owner._stamp_to_ns(msg.header.stamp)
        received_monotonic = time.monotonic()
        with self.owner.camera_frame_lock:
            version = self.owner.camera_frame_versions.get(camera_name, 0) + 1
            self.owner.camera_frame_versions[camera_name] = version
        if topic_type == "compressed":
            data = bytes(msg.data)
            self._encoding_paths[camera_name] = ("source-jpeg", time.monotonic())
            width, height = jpeg_dimensions(data)
            return CameraFrame(
                data=data,
                stamp_ns=stamp_ns,
                received_monotonic=received_monotonic,
                mime_type="image/jpeg",
                width=width,
                height=height,
                version=version,
            )
        if self.owner._hw_jpeg is None:
            raise RuntimeError("Hardware JPEG encoding is unavailable")
        with track(f"image_encode_hw:{camera_name}"):
            data, width, height = self.owner._hw_jpeg.encode_ros_image(camera_name, msg, quality=82)
        self._encoding_paths[camera_name] = ("jpeg-hardware-nvjpeg", time.monotonic())
        return CameraFrame(
            data=data,
            stamp_ns=stamp_ns,
            received_monotonic=received_monotonic,
            mime_type="image/jpeg",
            width=width,
            height=height,
            version=version,
        )
