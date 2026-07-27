"""Latest-frame image processing, preview encoding, and decode helpers."""

from __future__ import annotations

import time
from typing import Optional, Tuple

import cv2
import numpy as np

try:
    import rclpy
    from sensor_msgs.msg import Image as RosImage
except Exception:  # pragma: no cover - fake mode can run without ROS imports
    rclpy = None
    RosImage = None

from perf_tracker import track

from .models import CameraFrame

WEBRTC_JPEG_FALLBACK_INTERVAL_SEC = 0.5
RECORDING_WEBRTC_PREVIEW_FPS = 10.0
LOCALIZATION_IMAGE_RELAY_INTERVAL_NS = 500_000_000


class ImagePipeline:
    def __init__(self, owner) -> None:
        self.owner = owner

    def _make_dashboard_image_callback(
        self, camera_name: str, topic_type: str, also_alignment: bool = False, is_live: bool = True
    ):
        camera_topic = next(c.topic for c in self.owner.cameras if c.name == camera_name)
        event = self.owner._pending_frame_events[camera_name]

        # Keep callbacks near-zero cost so recording frames reach the queue.
        # Live and /bagplay sources share a slot but are gated by playback mode.
        def callback(msg) -> None:
            if is_live == self.owner._playback_mode:
                return
            if not self.owner._playback_mode:
                self.owner._feed_recording_writer(camera_topic, msg)
                self._maybe_relay_localization_image(camera_name, msg)
            self.owner._pending_frames[camera_name] = msg
            event.set()

        return callback

    def _maybe_relay_localization_image(self, camera_name: str, msg: object) -> None:
        """Reuse the display reader instead of adding another full-rate DDS reader."""

        publisher = self.owner._localization_image_publishers.get(camera_name)
        if publisher is None:
            return
        stamp_ns = self.owner._stamp_to_ns(msg.header.stamp)
        previous = self.owner._last_localization_image_relay_ns.get(camera_name, -1)
        if previous >= 0 and stamp_ns > previous:
            if stamp_ns - previous < LOCALIZATION_IMAGE_RELAY_INTERVAL_NS:
                return
        self.owner._last_localization_image_relay_ns[camera_name] = stamp_ns
        publisher.publish(msg)

    def _frame_worker_loop(self, camera_name: str, topic_type: str, also_alignment: bool) -> None:
        alignment_cb = self.owner._make_live_alignment_image_callback(camera_name, topic_type) if also_alignment else None
        event = self.owner._pending_frame_events[camera_name]
        while rclpy is not None and rclpy.ok():
            if not event.wait(timeout=1.0):
                continue
            event.clear()
            msg = self.owner._pending_frames.pop(camera_name, None)
            if msg is None:
                continue
            try:
                # Throttle WebRTC previews before allowing capture backpressure.
                preview_now = time.monotonic()
                if self.owner._recording_active() and self.owner._webrtc_has_sessions.get(camera_name):
                    min_interval = 1.0 / RECORDING_WEBRTC_PREVIEW_FPS
                    previous = self.owner._last_recording_preview_at.get(camera_name, 0.0)
                    if preview_now - previous < min_interval:
                        continue
                    self.owner._last_recording_preview_at[camera_name] = preview_now
                if alignment_cb is not None:
                    alignment_cb(msg)
                # Share one decode; skip CPU decode when NVJPEG can consume raw data.
                display_image = None
                if topic_type != "compressed" and (
                    camera_name in self.owner.gripper_tracking_cameras
                    or self.owner._hw_jpeg is None
                    or not self.owner._hw_jpeg.can_encode_ros_image(camera_name, msg)
                ):
                    display_image = self.owner._decode_display_image(msg)
                if camera_name in self.owner.gripper_tracking_cameras:
                    gripper_image = (
                        display_image
                        if display_image is not None
                        else self.owner._decode_calibration_message(topic_type, msg)
                    )
                    if gripper_image is not None:
                        self.owner._process_gripper_image(camera_name, gripper_image)
                now = time.monotonic()
                # Refresh the hidden HTTP fallback at low rate during WebRTC.
                refresh_fallback = (
                    topic_type == "compressed"
                    or not self.owner._webrtc_has_sessions.get(camera_name)
                    or now - self.owner._last_webrtc_fallback_jpeg_at.get(camera_name, 0.0)
                    >= WEBRTC_JPEG_FALLBACK_INTERVAL_SEC
                )
                frame = (
                    self.owner._encode_dashboard_frame(camera_name, topic_type, msg, display_image)
                    if refresh_fallback
                    else None
                )
                with self.owner.camera_frame_lock:
                    # Keep the prior composite visible until its replacement returns.
                    if frame is not None and not frame.hand_overlay_pending:
                        self.owner.latest_camera_frames[camera_name] = frame
                        if topic_type != "compressed":
                            self.owner._last_webrtc_fallback_jpeg_at[camera_name] = frame.received_monotonic
                    self.owner.camera_frame_times[camera_name].append(now)
                self.owner._maybe_queue_webrtc_frame(camera_name, topic_type, msg, frame)
            except Exception as exc:  # noqa: BLE001 - a bad frame must not kill the worker
                self.owner.get_logger().warning(f"frame worker {camera_name}: {exc}")

    def _encode_dashboard_frame(
        self,
        camera_name: str,
        topic_type: str,
        msg: object,
        decoded_image: Optional[np.ndarray] = None,
    ) -> Optional[CameraFrame]:
        stamp_ns = self.owner._stamp_to_ns(msg.header.stamp)
        received_monotonic = time.monotonic()
        with self.owner.camera_frame_lock:
            version = self.owner.camera_frame_versions.get(camera_name, 0) + 1
            self.owner.camera_frame_versions[camera_name] = version
        if topic_type == "compressed":
            data = bytes(msg.data)
            hand_overlay_pending = False
            if camera_name in self.owner.hand_overlay_enabled:
                # Dispatch overlay work asynchronously and serve the source meanwhile.
                hand_overlay_pending = self.owner.compose_hand_overlay_jpeg(camera_name, data, version)
            width, height = self.owner._jpeg_dimensions(data)
            return CameraFrame(
                data=data,
                stamp_ns=stamp_ns,
                received_monotonic=received_monotonic,
                mime_type="image/jpeg",
                width=width,
                height=height,
                version=version,
                hand_overlay_pending=hand_overlay_pending,
            )
        if self.owner._hw_jpeg is not None:
            with track(f"image_encode_hw:{camera_name}"):
                hw_result = self.owner._hw_jpeg.encode_ros_image(camera_name, msg, quality=82)
            if hw_result is not None:
                data, width, height = hw_result
                return CameraFrame(
                    data=data,
                    stamp_ns=stamp_ns,
                    received_monotonic=received_monotonic,
                    mime_type="image/jpeg",
                    width=width,
                    height=height,
                    version=version,
                )
        image = decoded_image if decoded_image is not None else self.owner._decode_display_image(msg)
        if image is None:
            return None
        with track(f"image_encode:{camera_name}"):
            ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
        if not ok:
            return None
        height, width = image.shape[:2]
        return CameraFrame(
            data=encoded.tobytes(),
            stamp_ns=stamp_ns,
            received_monotonic=received_monotonic,
            mime_type="image/jpeg",
            width=int(width),
            height=int(height),
            version=version,
        )

    def _decode_display_image(self, msg: object) -> Optional[np.ndarray]:
        # Keep mono8 direct; route NV12 through full YUV-to-BGR conversion.
        if isinstance(msg, RosImage) and msg.width > 0:
            encoding = msg.encoding.lower()
            if encoding in ("mono8", "8uc1"):
                data = np.frombuffer(msg.data, dtype=np.uint8)
                return data.reshape((msg.height, msg.width))
        return self.owner._decode_calibration_message("image", msg)

    @staticmethod
    def _jpeg_dimensions(data: bytes) -> Tuple[int, int]:
        # Fast JPEG SOF scan so compressed display does not need a full decode.
        try:
            index = 2
            length = len(data)
            while index + 9 < length:
                if data[index] != 0xFF:
                    index += 1
                    continue
                marker = data[index + 1]
                index += 2
                if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
                    continue
                if index + 2 > length:
                    break
                segment_length = int.from_bytes(data[index:index + 2], byteorder="big")
                if segment_length < 2 or index + segment_length > length:
                    break
                if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                    height = int.from_bytes(data[index + 3:index + 5], byteorder="big")
                    width = int.from_bytes(data[index + 5:index + 7], byteorder="big")
                    return width, height
                index += segment_length
        except Exception:
            pass
        return 0, 0
