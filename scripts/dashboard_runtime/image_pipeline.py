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


class ImagePipeline:
    def __init__(self, owner) -> None:
        self.owner = owner

    def _make_dashboard_image_callback(
        self, camera_name: str, topic_type: str, also_alignment: bool = False, is_live: bool = True
    ):
        camera_topic = next(c.topic for c in self.owner.cameras if c.name == camera_name)
        event = self.owner._pending_frame_events[camera_name]

        # The subscription callback must stay near-zero cost: anything heavy
        # here (gripper ArUco detection alone was measured at 35-48% of a
        # core per camera) pushes per-frame handling past the 50ms frame
        # period, the DDS receive queue overflows, and messages are dropped
        # before recording ever sees them. So this only feeds the recording
        # writer (a queue put) and stashes the latest message for the
        # per-camera worker thread, which does gripper/alignment/encode at
        # whatever rate it can manage -- skipping display frames is fine,
        # losing recorded frames is not.
        #
        # Live and playback are two SEPARATE subscriptions on two different
        # topics (playback's is remapped to /bagplay/... by PlaybackManager),
        # both funneling into this same per-camera pending-frame slot. Exactly
        # one side is ever authoritative: is_live == self.owner._playback_mode means
        # "wrong source for the current mode" -- a live camera still
        # connected during playback, or a stray playback message lingering
        # after playback stopped -- so it's dropped before display, never
        # blended by timestamp guessing (camera header stamps are boot-
        # relative, not epoch, so they can't disambiguate the two).
        def callback(msg) -> None:
            if is_live == self.owner._playback_mode:
                return
            if not self.owner._playback_mode:
                self.owner._feed_recording_writer(camera_topic, msg)
            self.owner._pending_frames[camera_name] = msg
            event.set()

        return callback

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
                # The subscription callback above has already put this frame
                # on the recording queue. During a recording, rendering and
                # copying every 1080p JPEG into the WebRTC IPC channel can
                # monopolize the Python process long enough for DDS history
                # to backpressure the publisher. Keep a responsive preview
                # but deliberately sacrifice preview cadence before capture
                # cadence. This is only relevant while a browser has a
                # WebRTC session; without one the existing fallback is cheap.
                preview_now = time.monotonic()
                if self.owner._recording_active() and self.owner._webrtc_has_sessions.get(camera_name):
                    min_interval = 1.0 / RECORDING_WEBRTC_PREVIEW_FPS
                    previous = self.owner._last_recording_preview_at.get(camera_name, 0.0)
                    if preview_now - previous < min_interval:
                        continue
                    self.owner._last_recording_preview_at[camera_name] = preview_now
                if alignment_cb is not None:
                    alignment_cb(msg)
                # Decode once and share: the display path and the gripper
                # detector both work on the same pixels (the detector accepts
                # 2-D grayscale directly and converts BGR itself otherwise),
                # so a second full decode of the identical message is waste.
                # When NVJPEG will encode the raw NV12/mono8 bytes directly
                # and no gripper detector needs BGR pixels, skip the CPU
                # decode entirely -- nvvidconv does the color conversion in
                # hardware on the way into the encoder.
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
                # Compressed input is already a JPEG, but raw input would
                # otherwise pay an NVJPEG encode for every frame solely for
                # an HTTP fallback hidden behind an active WebRTC <video>.
                # Refresh that fallback twice a second instead.
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
                    # Once an overlay has been dispatched, keep the last
                    # composited frame onscreen until its replacement returns
                    # from the worker. Replacing it with every raw source
                    # frame made the overlay visible only for the fraction of
                    # a frame between the asynchronous result and the next
                    # camera callback.
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
                # Gates the frame and, if worth overlaying, dispatches the
                # actual decode/draw/re-encode to hand_overlay_worker.py --
                # see compose_hand_overlay_jpeg's docstring for why that work
                # can't happen inline here anymore. This tick still serves
                # the plain passthrough `data`; the composited version lands
                # asynchronously and patches into latest_camera_frames once
                # ready (_hand_overlay_ipc_loop), so a served frame is
                # undecorated for at most one IPC round trip.
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
        # Display-only decode for raw (non-compressed) streams. mono8/8uc1
        # is genuinely single-channel at the format level (no chroma exists
        # to discard), so that shortcut stays. NV12 previously took the same
        # Y-plane-only shortcut on the assumption its chroma was always
        # neutral -- live insight3_b samples confirmed real per-frame U/V
        # content, so that assumption doesn't hold in general. Route it
        # through the shared full YUV->BGR decoder instead so no color data
        # is silently dropped.
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
