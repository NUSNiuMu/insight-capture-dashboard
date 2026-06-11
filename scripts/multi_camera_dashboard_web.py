#!/usr/bin/env python3

import argparse
import asyncio
import contextlib
import json
import math
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Deque, Dict, List, Optional, Set, Tuple
from urllib.parse import quote

import cv2
import numpy as np
from aiohttp import web

try:
    import rclpy
    from geometry_msgs.msg import PoseStamped
    from rclpy.callback_groups import ReentrantCallbackGroup
    from rclpy.executors import MultiThreadedExecutor
    from rclpy.node import Node
    from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
    from sensor_msgs.msg import CameraInfo
    from sensor_msgs.msg import CompressedImage, Image as RosImage
except Exception:  # pragma: no cover - fake mode can run without ROS imports
    rclpy = None
    PoseStamped = None
    ReentrantCallbackGroup = None
    MultiThreadedExecutor = None
    Node = object
    QoSProfile = None
    ReliabilityPolicy = None
    HistoryPolicy = None
    DurabilityPolicy = None
    CameraInfo = None
    CompressedImage = None
    RosImage = None

from camera_setup import IMAGE_STREAMS, build_dashboard_config, camera_info_topic, image_topic, load_setup
from live_alignment import LiveAlignmentMixin
from session_alignment import PoseSample


def make_qos(depth: int = 10) -> QoSProfile:
    return QoSProfile(
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.VOLATILE,
        history=HistoryPolicy.KEEP_LAST,
        depth=depth,
    )


def make_image_qos(depth: int = 1, reliability: str = "best_effort") -> QoSProfile:
    reliability_policy = (
        ReliabilityPolicy.RELIABLE
        if str(reliability).lower() == "reliable"
        else ReliabilityPolicy.BEST_EFFORT
    )
    return QoSProfile(
        reliability=reliability_policy,
        durability=DurabilityPolicy.VOLATILE,
        history=HistoryPolicy.KEEP_LAST,
        depth=depth,
    )


@dataclass
class PoseSpec:
    name: str
    topic: str
    color: str
    teleop_role: str
    avatar_model: Optional[str]
    avatar_scale: float


@dataclass
class CameraSpec:
    name: str
    label: str
    namespace: str
    topic: str
    camera_info_topic: str
    topic_type: str
    rotation_deg: int
    row: int
    column: int
    column_span: int
    row_span: int


class PoseBridgeNode(LiveAlignmentMixin, Node):
    def __init__(self, config_path: Path, fake_pose: bool = False, pose_publish_hz: float = 30.0) -> None:
        if rclpy is None:
            raise RuntimeError("rclpy is required to run the web dashboard backend")
        super().__init__("insight_multi_camera_dashboard_web")
        self.config_path = config_path
        self.fake_pose = bool(fake_pose)
        self.pose_publish_hz = max(1.0, float(pose_publish_hz))
        self.max_points = 300
        self.pose_timeout_sec = 0.5
        self.preview_jpeg_quality = 78
        self.preview_max_width = 640
        self.preview_passthrough_compressed = True

        raw_config = load_setup(config_path)
        config = build_dashboard_config(raw_config)
        enabled_camera_map = {
            camera["name"]: camera for camera in raw_config.get("cameras", []) if camera.get("enabled", True)
        }
        self.project_root = config_path.resolve().parents[1]
        self.window_title = config.get("window_title", "Insight Web Dashboard")
        self.image_qos_reliability = str(config.get("trajectory", {}).get("image_qos_reliability", "best_effort"))
        self._configure_live_alignment(raw_config, config)

        self.cameras: List[CameraSpec] = [
            CameraSpec(
                name=item["name"],
                label=item["label"],
                namespace=enabled_camera_map[item["name"]]["namespace"],
                topic=item["topic"],
                camera_info_topic=item["camera_info_topic"],
                topic_type=item["type"],
                rotation_deg=int(item.get("rotation_deg", 0)),
                row=int(item.get("row", 0)),
                column=int(item.get("column", 0)),
                column_span=int(item.get("column_span", 1)),
                row_span=int(item.get("row_span", 1)),
            )
            for item in config.get("cameras", [])
        ]
        self.poses: List[PoseSpec] = [
            PoseSpec(
                name=item["name"],
                topic=item["topic"],
                color=item["color"],
                teleop_role=str(item.get("teleop_role", item["name"])),
                avatar_model=item.get("avatar_model"),
                avatar_scale=float(item.get("avatar_scale", 1.0)),
            )
            for item in config.get("poses", [])
        ]
        if self.reference_camera is None and self.poses:
            self.reference_camera = self.poses[0].name

        self.raw_traces: Dict[str, List[Tuple[float, float, float]]] = {pose.name: [] for pose in self.poses}
        self.latest_pose: Dict[str, Optional[Tuple[float, float, float]]] = {pose.name: None for pose in self.poses}
        self.latest_pose_sample: Dict[str, Optional[PoseSample]] = {pose.name: None for pose in self.poses}
        self.last_pose_received_time: Dict[str, float] = {pose.name: 0.0 for pose in self.poses}
        self.pose_versions: Dict[str, int] = {pose.name: 0 for pose in self.poses}
        self.pose_history: Dict[str, Deque[PoseSample]] = {pose.name: deque(maxlen=160) for pose in self.poses}
        self.pose_history_lock = threading.Lock()
        self.pose_lock = threading.Lock()
        self.image_lock = threading.Lock()
        self.pending_lock = threading.Lock()
        self.live_alignment_image_lock = threading.Lock()
        self.live_alignment_solution_lock = threading.Lock()
        self.ros_callback_group = ReentrantCallbackGroup()
        self.dashboard_subscriptions = []
        self.pending_messages: Dict[str, Optional[object]] = {camera.name: None for camera in self.cameras}
        self.decoder_events: Dict[str, threading.Event] = {camera.name: threading.Event() for camera in self.cameras}
        self.decoder_stop_event = threading.Event()
        self.decoder_threads: List[threading.Thread] = []
        self.latest_images: Dict[str, Optional[np.ndarray]] = {camera.name: None for camera in self.cameras}
        self.latest_preview_jpeg: Dict[str, Optional[bytes]] = {camera.name: None for camera in self.cameras}
        self.latest_image_shapes: Dict[str, Optional[Tuple[int, int]]] = {camera.name: None for camera in self.cameras}
        self.image_versions: Dict[str, int] = {camera.name: 0 for camera in self.cameras}
        self.last_frame_received_time: Dict[str, float] = {camera.name: 0.0 for camera in self.cameras}
        self.last_frame_decoded_time: Dict[str, float] = {camera.name: 0.0 for camera in self.cameras}
        self.frame_timestamps: Dict[str, Deque[float]] = {camera.name: deque(maxlen=60) for camera in self.cameras}
        self._initialize_live_alignment_state()

        for camera in self.cameras:
            if camera.topic_type == "compressed" and self.preview_passthrough_compressed:
                continue
            worker = threading.Thread(
                target=self._decoder_worker,
                args=(camera,),
                daemon=True,
                name=f"{camera.name}_decoder",
            )
            worker.start()
            self.decoder_threads.append(worker)

        if self.fake_pose:
            self.create_timer(1.0 / self.pose_publish_hz, self._update_fake_pose, callback_group=self.ros_callback_group)
            self.get_logger().info("Running in fake-pose demo mode")
        else:
            self._create_pose_subscriptions()
            self._create_image_subscriptions()
            self._create_alignment_subscriptions()

        if self.live_alignment_available:
            self.live_alignment_timer = self.create_timer(
                1.0 / max(self.live_alignment_processing_hz, 0.5),
                self._process_live_alignment,
                callback_group=self.ros_callback_group,
            )

    def _create_pose_subscriptions(self) -> None:
        pose_qos = make_qos()
        for pose in self.poses:
            sub = self.create_subscription(
                PoseStamped,
                pose.topic,
                self._make_pose_callback(pose.name),
                pose_qos,
                callback_group=self.ros_callback_group,
            )
            self.dashboard_subscriptions.append(sub)
            self.get_logger().info(f"Trajectory: {pose.name} <- {pose.topic}")

    def _create_image_subscriptions(self) -> None:
        image_qos = make_image_qos(reliability=self.image_qos_reliability)
        for camera in self.cameras:
            if camera.topic_type == "compressed":
                sub = self.create_subscription(
                    CompressedImage,
                    camera.topic,
                    self._make_compressed_callback(camera.name),
                    image_qos,
                    callback_group=self.ros_callback_group,
                )
            else:
                sub = self.create_subscription(
                    RosImage,
                    camera.topic,
                    self._make_image_callback(camera.name),
                    image_qos,
                    callback_group=self.ros_callback_group,
                )
            self.dashboard_subscriptions.append(sub)
            self.get_logger().info(
                f"Image: {camera.label} <- {camera.topic} "
                f"(qos={self.image_qos_reliability}, depth={image_qos.depth})"
            )

    def _create_alignment_subscriptions(self) -> None:
        if not self.live_alignment_available:
            return
        image_qos = make_image_qos(reliability=self.image_qos_reliability)
        for camera in self.cameras:
            camera_name = camera.name
            namespace = camera.namespace
            calib_topic = image_topic(namespace, self.live_alignment_image_stream)
            calib_info_topic = camera_info_topic(namespace, self.live_alignment_image_stream)
            calib_type = IMAGE_STREAMS[self.live_alignment_image_stream]["type"]
            self.live_alignment_topic_by_camera[camera_name] = calib_topic

            calib_msg_type = CompressedImage if calib_type == "compressed" else RosImage
            calib_sub = self.create_subscription(
                calib_msg_type,
                calib_topic,
                self._make_live_alignment_image_callback(camera_name, calib_type),
                image_qos,
                callback_group=self.ros_callback_group,
            )
            self.dashboard_subscriptions.append(calib_sub)

            info_sub = self.create_subscription(
                CameraInfo,
                calib_info_topic,
                self._make_camera_info_callback(camera_name),
                make_qos(depth=2),
                callback_group=self.ros_callback_group,
            )
            self.dashboard_subscriptions.append(info_sub)
            self.get_logger().info(
                f"Alignment: {camera_name} image={calib_topic} info={calib_info_topic} type={calib_type}"
            )

    def _make_image_callback(self, camera_name: str):
        def callback(msg: RosImage) -> None:
            self._queue_frame(camera_name, msg)

        return callback

    def _make_compressed_callback(self, camera_name: str):
        def callback(msg: CompressedImage) -> None:
            if self.preview_passthrough_compressed:
                self._store_passthrough_frame(camera_name, msg)
                return
            self._queue_frame(camera_name, msg)

        return callback

    def _make_pose_callback(self, pose_name: str):
        def callback(msg: PoseStamped) -> None:
            pose_sample = PoseSample(
                stamp_ns=self._stamp_to_ns(msg.header.stamp),
                position=(
                    float(msg.pose.position.x),
                    float(msg.pose.position.y),
                    float(msg.pose.position.z),
                ),
                orientation_xyzw=(
                    float(msg.pose.orientation.x),
                    float(msg.pose.orientation.y),
                    float(msg.pose.orientation.z),
                    float(msg.pose.orientation.w),
                ),
            )
            self._record_pose_sample(pose_name, pose_sample)

        return callback

    def _make_camera_info_callback(self, camera_name: str):
        def callback(msg: CameraInfo) -> None:
            self.live_alignment_camera_matrix[camera_name] = np.array(msg.k, dtype=np.float64).reshape((3, 3))
            self.live_alignment_dist_coeffs[camera_name] = np.array(msg.d, dtype=np.float64).reshape((-1, 1))

        return callback

    def _queue_frame(self, camera_name: str, msg: object) -> None:
        self.last_frame_received_time[camera_name] = time.monotonic()
        with self.pending_lock:
            self.pending_messages[camera_name] = msg
        self.decoder_events[camera_name].set()

    def _store_passthrough_frame(self, camera_name: str, msg: CompressedImage) -> None:
        self.last_frame_received_time[camera_name] = time.monotonic()
        preview = self._build_passthrough_preview(camera_name, msg)
        if preview is None:
            return
        jpeg, shape = preview
        now = time.monotonic()
        with self.image_lock:
            self.latest_images[camera_name] = None
            self.latest_preview_jpeg[camera_name] = jpeg
            self.latest_image_shapes[camera_name] = shape
            self.image_versions[camera_name] += 1
            self.frame_timestamps[camera_name].append(now)
        self.last_frame_decoded_time[camera_name] = now

    def _decoder_worker(self, camera: CameraSpec) -> None:
        event = self.decoder_events[camera.name]
        while not self.decoder_stop_event.is_set():
            event.wait(0.2)
            if self.decoder_stop_event.is_set():
                break
            if not event.is_set():
                continue
            event.clear()
            with self.pending_lock:
                msg = self.pending_messages[camera.name]
                self.pending_messages[camera.name] = None
            if msg is None:
                continue

            if camera.topic_type == "compressed" and self.preview_passthrough_compressed:
                preview = self._build_passthrough_preview(camera.name, msg)
                if preview is not None:
                    jpeg, shape = preview
                    with self.image_lock:
                        self.latest_images[camera.name] = None
                        self.latest_preview_jpeg[camera.name] = jpeg
                        self.latest_image_shapes[camera.name] = shape
                        self.image_versions[camera.name] += 1
                        self.frame_timestamps[camera.name].append(time.monotonic())
                    self.last_frame_decoded_time[camera.name] = time.monotonic()
                    continue

            try:
                image = self._decode_message(camera, msg)
            except Exception as exc:
                self.get_logger().warning(f"Decoder worker failed for {camera.name}: {exc}")
                continue
            if image is None:
                continue
            jpeg = self._encode_preview_jpeg(image)
            with self.image_lock:
                self.latest_images[camera.name] = image
                self.latest_preview_jpeg[camera.name] = jpeg
                self.latest_image_shapes[camera.name] = (int(image.shape[1]), int(image.shape[0]))
                self.image_versions[camera.name] += 1
                self.frame_timestamps[camera.name].append(time.monotonic())
            self.last_frame_decoded_time[camera.name] = time.monotonic()

    def _make_live_alignment_image_callback(self, camera_name: str, topic_type: str):
        def callback(msg) -> None:
            if not self.live_alignment_active:
                return
            image = self._decode_calibration_message(topic_type, msg)
            if image is None:
                self.live_alignment_last_tag_count[camera_name] = 0
                self._set_alignment_debug(camera_name, stage="decode_failed", tags=0, shape="-")
                return
            stamp_ns = self._stamp_to_ns(msg.header.stamp)
            received_monotonic_ns = time.monotonic_ns()
            with self.live_alignment_image_lock:
                self.live_alignment_latest_image[camera_name] = image
                self.live_alignment_latest_image_stamp_ns[camera_name] = stamp_ns
                pending = self.live_alignment_pending_images[camera_name]
                if not pending or pending[-1][0] != stamp_ns:
                    pending.append((stamp_ns, received_monotonic_ns, image))
                    min_received_ns = received_monotonic_ns - self.live_alignment_pending_max_age_ns
                    pending[:] = [item for item in pending if item[1] >= min_received_ns]
                    if len(pending) > self.live_alignment_pending_image_limit:
                        del pending[: len(pending) - self.live_alignment_pending_image_limit]
            self._set_alignment_debug(
                camera_name,
                stage="image_rx",
                stamp_ns=stamp_ns,
                shape=f"{image.shape[1]}x{image.shape[0]}",
                topic=self.live_alignment_topic_by_camera.get(camera_name, "-"),
                latency_ms=f"{0.0:.1f}",
            )

        return callback

    def _decode_calibration_message(self, topic_type: str, msg) -> Optional[np.ndarray]:
        if topic_type == "compressed":
            return self._decode_compressed_image(msg)
        return self._convert_ros_image(msg)

    def _decode_message(self, camera: CameraSpec, msg: object) -> Optional[np.ndarray]:
        if camera.topic_type == "compressed":
            return self._decode_compressed_pil(msg)
        return self._convert_ros_image(msg)

    def _decode_compressed_pil(self, msg: CompressedImage) -> Optional[np.ndarray]:
        try:
            from PIL import Image
        except Exception:
            return self._decode_compressed_image(msg)
        try:
            with Image.open(BytesIO(msg.data)) as img:
                img = img.convert("RGB")
                image = np.array(img, dtype=np.uint8)
                return np.ascontiguousarray(image)
        except Exception:
            return self._decode_compressed_image(msg)

    def _decode_compressed_image(self, msg: CompressedImage) -> Optional[np.ndarray]:
        buffer = np.frombuffer(msg.data, dtype=np.uint8)
        image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
        if image is None:
            return None
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        return np.ascontiguousarray(rgb)

    def _convert_ros_image(self, msg: RosImage) -> Optional[np.ndarray]:
        if msg.width == 0 or msg.height == 0:
            return None
        data = np.frombuffer(msg.data, dtype=np.uint8)
        encoding = msg.encoding.lower()
        channels_by_encoding = {
            "mono8": 1,
            "8uc1": 1,
            "rgb8": 3,
            "bgr8": 3,
            "rgba8": 4,
            "bgra8": 4,
        }
        channels = channels_by_encoding.get(encoding)
        if channels is None:
            channels = max(msg.step // max(msg.width, 1), 1)
        if msg.step <= 0 or data.size < msg.step * msg.height:
            return None
        row_bytes = msg.step
        image = data[: row_bytes * msg.height].reshape((msg.height, row_bytes))
        if encoding in ("mono8", "8uc1"):
            gray = image[:, : msg.width]
            rgb = np.repeat(gray[:, :, None], 3, axis=2).astype(np.uint8, copy=False)
            return np.ascontiguousarray(rgb)
        if encoding == "rgb8":
            rgb = image[:, : msg.width * 3].reshape((msg.height, msg.width, 3))
            return np.ascontiguousarray(rgb.astype(np.uint8, copy=False))
        if encoding == "bgr8":
            bgr = image[:, : msg.width * 3].reshape((msg.height, msg.width, 3))
            return np.ascontiguousarray(bgr[:, :, ::-1].astype(np.uint8, copy=False))
        if encoding == "rgba8":
            rgba = image[:, : msg.width * 4].reshape((msg.height, msg.width, 4))
            return np.ascontiguousarray(rgba[:, :, :3].astype(np.uint8, copy=False))
        if encoding == "bgra8":
            bgra = image[:, : msg.width * 4].reshape((msg.height, msg.width, 4))
            rgb = bgra[:, :, [2, 1, 0]]
            return np.ascontiguousarray(rgb.astype(np.uint8, copy=False))
        pixel_image = image[:, : msg.width * channels].reshape((msg.height, msg.width, channels))
        if channels >= 3:
            return np.ascontiguousarray(pixel_image[:, :, :3].astype(np.uint8, copy=False))
        if channels == 1:
            gray = pixel_image[:, :, 0]
            rgb = np.repeat(gray[:, :, None], 3, axis=2).astype(np.uint8, copy=False)
            return np.ascontiguousarray(rgb)
        return None

    def _build_passthrough_preview(
        self,
        camera_name: str,
        msg: CompressedImage,
    ) -> Optional[Tuple[bytes, Tuple[int, int]]]:
        jpeg = bytes(msg.data)
        if not jpeg:
            return None
        shape = self.latest_image_shapes.get(camera_name)
        if shape is None:
            shape = self._jpeg_size(jpeg)
        if shape is None:
            return None
        width, height = int(shape[0]), int(shape[1])
        return jpeg, (width, height)

    @staticmethod
    def _jpeg_size(jpeg: bytes) -> Optional[Tuple[int, int]]:
        if len(jpeg) < 4 or jpeg[0] != 0xFF or jpeg[1] != 0xD8:
            return None
        offset = 2
        sof_markers = {
            0xC0, 0xC1, 0xC2, 0xC3,
            0xC5, 0xC6, 0xC7,
            0xC9, 0xCA, 0xCB,
            0xCD, 0xCE, 0xCF,
        }
        length = len(jpeg)
        while offset + 9 < length:
            if jpeg[offset] != 0xFF:
                offset += 1
                continue
            marker = jpeg[offset + 1]
            offset += 2
            while marker == 0xFF and offset < length:
                marker = jpeg[offset]
                offset += 1
            if marker in (0xD8, 0xD9):
                continue
            if offset + 2 > length:
                return None
            segment_length = (jpeg[offset] << 8) | jpeg[offset + 1]
            if segment_length < 2 or offset + segment_length > length:
                return None
            if marker in sof_markers:
                if offset + 7 > length:
                    return None
                height = (jpeg[offset + 3] << 8) | jpeg[offset + 4]
                width = (jpeg[offset + 5] << 8) | jpeg[offset + 6]
                return (int(width), int(height))
            offset += segment_length
        return None

    def _encode_preview_jpeg(self, image: np.ndarray) -> bytes:
        rgb = image
        height, width = rgb.shape[:2]
        if width > self.preview_max_width:
            scale = self.preview_max_width / float(width)
            new_size = (self.preview_max_width, max(1, int(round(height * scale))))
            rgb = cv2.resize(rgb, new_size, interpolation=cv2.INTER_AREA)
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        ok, encoded = cv2.imencode(
            ".jpg",
            bgr,
            [int(cv2.IMWRITE_JPEG_QUALITY), int(self.preview_jpeg_quality)],
        )
        if not ok:
            raise RuntimeError("failed to encode jpeg preview")
        return encoded.tobytes()

    def build_camera_payload(self) -> Dict[str, object]:
        now = time.monotonic()
        cameras = []
        with self.image_lock:
            for camera in self.cameras:
                cameras.append(
                    {
                        "name": camera.name,
                        "label": camera.label,
                        "visible": self.latest_preview_jpeg[camera.name] is not None,
                        "frame_url": f"/frame/{camera.name}.jpg",
                        "version": self.image_versions[camera.name],
                        "width": self.latest_image_shapes[camera.name][0] if self.latest_image_shapes[camera.name] else None,
                        "height": self.latest_image_shapes[camera.name][1] if self.latest_image_shapes[camera.name] else None,
                        "fps": self._camera_fps(camera.name, now),
                        "rotation_deg": camera.rotation_deg,
                        "row": camera.row,
                        "column": camera.column,
                        "row_span": camera.row_span,
                        "column_span": camera.column_span,
                        "stale": (now - self.last_frame_decoded_time[camera.name]) > 1.0 if self.last_frame_decoded_time[camera.name] else True,
                    }
                )
        return {"type": "camera_update", "cameras": cameras}

    def get_camera_frame(self, camera_name: str) -> Optional[bytes]:
        with self.image_lock:
            data = self.latest_preview_jpeg.get(camera_name)
        if data is not None:
            return data
        return self._placeholder_frame(camera_name)

    def _camera_fps(self, camera_name: str, now: float) -> float:
        samples = self.frame_timestamps.get(camera_name)
        if not samples:
            return 0.0
        min_time = now - 1.5
        while samples and samples[0] < min_time:
            samples.popleft()
        if len(samples) < 2:
            return 0.0
        duration = max(samples[-1] - samples[0], 1e-3)
        return float((len(samples) - 1) / duration)

    def _placeholder_frame(self, camera_name: str) -> bytes:
        canvas = np.full((270, 480, 3), 18, dtype=np.uint8)
        cv2.putText(canvas, "No image", (140, 120), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (220, 220, 220), 2, cv2.LINE_AA)
        cv2.putText(canvas, camera_name, (140, 160), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (140, 180, 220), 2, cv2.LINE_AA)
        ok, encoded = cv2.imencode(".jpg", canvas, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        return encoded.tobytes() if ok else b""

    def _record_pose_sample(self, pose_name: str, pose_sample: PoseSample) -> None:
        with self.pose_history_lock:
            self.pose_history[pose_name].append(pose_sample)
        with self.pose_lock:
            self.latest_pose_sample[pose_name] = pose_sample
            self.latest_pose[pose_name] = self._transform_pose_point(pose_name, pose_sample.position)
            self.last_pose_received_time[pose_name] = time.monotonic()
            self.pose_versions[pose_name] += 1
            raw_trace = self.raw_traces[pose_name]
            raw_trace.append(pose_sample.position)
            if len(raw_trace) > self.max_points:
                del raw_trace[: len(raw_trace) - self.max_points]

    def _update_fake_pose(self) -> None:
        now = time.monotonic()
        roles = {
            "head": (0.0, 0.0, 1.45),
            "left_hand": (-0.35, 0.0, 1.10),
            "right_hand": (0.35, 0.0, 1.10),
        }
        phase = now * 0.9
        for pose in self.poses:
            base = roles.get(pose.teleop_role, (0.0, 0.0, 1.0))
            swing = 0.16 if pose.teleop_role != "head" else 0.08
            x = base[0] + swing * math.sin(phase + self._role_phase(pose.teleop_role))
            y = base[1] + 0.20 * math.cos(phase * 0.6 + self._role_phase(pose.name))
            z = base[2] + 0.08 * math.sin(phase * 1.4 + self._role_phase(pose.teleop_role) * 0.5)
            yaw = 0.45 * math.sin(phase * 0.7 + self._role_phase(pose.name))
            quaternion = self._yaw_quaternion(yaw)
            sample = PoseSample(
                stamp_ns=time.time_ns(),
                position=(x, y, z),
                orientation_xyzw=quaternion,
            )
            self._record_pose_sample(pose.name, sample)

    def build_pose_payload(self) -> Dict[str, object]:
        now = time.monotonic()
        poses = []
        with self.pose_lock:
            for pose in self.poses:
                transformed = self.transformed_pose_sample(pose.name)
                raw_sample = self.latest_pose_sample.get(pose.name)
                visible = raw_sample is not None and (self.fake_pose or (now - self.last_pose_received_time[pose.name]) <= self.pose_timeout_sec)
                if transformed is None:
                    position = [0.0, 0.0, 0.0]
                    quaternion = [0.0, 0.0, 0.0, 1.0]
                else:
                    position = [float(value) for value in transformed.position]
                    quaternion = [float(value) for value in transformed.orientation_xyzw]
                poses.append(
                    {
                        "name": pose.name,
                        "role": pose.teleop_role,
                        "visible": visible,
                        "position": position,
                        "quaternion_xyzw": quaternion,
                        "avatar_model": pose.avatar_model,
                        "avatar_scale": pose.avatar_scale,
                    }
                )
        return {
            "type": "pose_update",
            "timestamp_ms": int(time.time() * 1000),
            "fake_pose": self.fake_pose,
            "poses": poses,
        }

    def model_asset_url(self, avatar_model: Optional[str]) -> Optional[str]:
        if not avatar_model:
            return None
        return f"/asset?path={quote(avatar_model, safe='')}"

    def shutdown_workers(self) -> None:
        self.decoder_stop_event.set()
        for event in self.decoder_events.values():
            event.set()
        for worker in self.decoder_threads:
            worker.join(timeout=0.5)

    @staticmethod
    def _stamp_to_ns(stamp) -> int:
        return int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)

    @staticmethod
    def _role_phase(name: str) -> float:
        return (sum(ord(ch) for ch in name) % 17) * 0.19

    @staticmethod
    def _yaw_quaternion(yaw_rad: float) -> Tuple[float, float, float, float]:
        half = yaw_rad * 0.5
        return (0.0, 0.0, math.sin(half), math.cos(half))


class WebDashboardServer:
    def __init__(
        self,
        node: PoseBridgeNode,
        host: str,
        port: int,
        web_root: Optional[Path],
        project_root: Path,
    ) -> None:
        self.node = node
        self.host = host
        self.port = int(port)
        self.web_root = web_root.resolve() if web_root else None
        self.project_root = project_root.resolve()
        self._clients: Set[web.WebSocketResponse] = set()
        self._loop = asyncio.new_event_loop()
        self._thread: Optional[threading.Thread] = None
        self._started = threading.Event()
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="web_dashboard_server")
        self._thread.start()
        self._started.wait(timeout=5.0)

    def stop(self) -> None:
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=2.0)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        app = web.Application()
        app.router.add_get("/ws", self._handle_ws)
        app.router.add_get("/healthz", self._handle_healthz)
        app.router.add_get("/api/poses", self._handle_pose_snapshot)
        app.router.add_get("/api/cameras", self._handle_camera_snapshot)
        app.router.add_get(r"/frame/{camera_name}.jpg", self._handle_camera_frame)
        app.router.add_get(r"/stream/{camera_name}.mjpg", self._handle_camera_stream)
        app.router.add_get("/asset", self._handle_asset)
        if self.web_root and self.web_root.exists():
            app.router.add_get("/", self._handle_index)
            static_root = self.web_root / "static"
            if static_root.exists():
                app.router.add_static("/static/", str(static_root), show_index=False)
        app.on_startup.append(self._on_startup)
        app.on_shutdown.append(self._on_shutdown)
        runner = web.AppRunner(app)
        self._loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, self.host, self.port)
        self._loop.run_until_complete(site.start())
        self._started.set()
        self.node.get_logger().info(f"Web dashboard backend listening on http://{self.host}:{self.port}")
        try:
            self._loop.run_forever()
        finally:
            self._loop.run_until_complete(runner.cleanup())
            self._loop.close()

    async def _on_startup(self, app: web.Application) -> None:
        app["broadcast_task"] = asyncio.create_task(self._broadcast_loop())

    async def _on_shutdown(self, app: web.Application) -> None:
        task = app.get("broadcast_task")
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _broadcast_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0 / self.node.pose_publish_hz)
            if not self._clients:
                continue
            payload = self.node.build_pose_payload()
            for pose in payload["poses"]:
                pose["asset_url"] = self.node.model_asset_url(pose.get("avatar_model"))
            stale = []
            for ws in list(self._clients):
                try:
                    await ws.send_json(payload)
                except Exception:
                    stale.append(ws)
            for ws in stale:
                self._clients.discard(ws)

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20.0)
        await ws.prepare(request)
        self._clients.add(ws)
        snapshot = self.node.build_pose_payload()
        for pose in snapshot["poses"]:
            pose["asset_url"] = self.node.model_asset_url(pose.get("avatar_model"))
        await ws.send_json(snapshot)
        async for _message in ws:
            pass
        self._clients.discard(ws)
        return ws

    async def _handle_healthz(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "fake_pose": self.node.fake_pose})

    async def _handle_pose_snapshot(self, _request: web.Request) -> web.Response:
        payload = self.node.build_pose_payload()
        for pose in payload["poses"]:
            pose["asset_url"] = self.node.model_asset_url(pose.get("avatar_model"))
        return web.json_response(payload)

    async def _handle_camera_snapshot(self, _request: web.Request) -> web.Response:
        return web.json_response(self.node.build_camera_payload())

    async def _handle_camera_frame(self, request: web.Request) -> web.StreamResponse:
        camera_name = request.match_info["camera_name"]
        data = self.node.get_camera_frame(camera_name)
        if data is None:
            raise web.HTTPNotFound(text="camera not found")
        return web.Response(
            body=data,
            content_type="image/jpeg",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )

    async def _handle_camera_stream(self, request: web.Request) -> web.StreamResponse:
        camera_name = request.match_info["camera_name"]
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=frame",
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Connection": "close",
            },
        )
        await response.prepare(request)
        last_version = -1
        try:
            while True:
                with self.node.image_lock:
                    version = self.node.image_versions.get(camera_name, -1)
                    frame = self.node.latest_preview_jpeg.get(camera_name)
                if frame is None:
                    frame = self.node._placeholder_frame(camera_name)
                if version != last_version:
                    payload = (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        + f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii")
                        + frame
                        + b"\r\n"
                    )
                    await response.write(payload)
                    last_version = version
                await asyncio.sleep(0.01)
        except (asyncio.CancelledError, ConnectionResetError, RuntimeError):
            pass
        finally:
            with contextlib.suppress(Exception):
                await response.write_eof()
        return response

    async def _handle_index(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.web_root / "index.html")

    async def _handle_asset(self, request: web.Request) -> web.StreamResponse:
        raw_path = request.query.get("path", "").strip()
        if not raw_path:
            raise web.HTTPBadRequest(text="missing path")
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = (self.project_root / candidate).resolve()
        else:
            candidate = candidate.resolve()
        allowed_roots = [self.project_root, self.project_root.parent]
        for root in allowed_roots:
            try:
                candidate.relative_to(root)
                break
            except ValueError:
                continue
        else:
            raise web.HTTPForbidden(text="path outside allowed roots")
        if not candidate.is_file():
            raise web.HTTPNotFound(text="asset not found")
        return web.FileResponse(candidate)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path(__file__).resolve().parents[1] / "config" / "cameras.json"))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--web-root", default=str(Path(__file__).resolve().parents[1] / "web_dashboard" / "dist"))
    parser.add_argument("--fake-pose", action="store_true")
    parser.add_argument("--pose-publish-hz", type=float, default=30.0)
    parser.add_argument("--start-alignment", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config_path = Path(args.config).resolve()
    raw_config = load_setup(config_path)
    ros_domain_id = int(raw_config.get("ros_domain_id", 10))
    if rclpy is None:
        raise RuntimeError("rclpy is not available in this environment")

    os.environ.setdefault("ROS_DOMAIN_ID", str(ros_domain_id))
    rclpy.init(args=None)
    node = PoseBridgeNode(config_path, fake_pose=args.fake_pose, pose_publish_hz=args.pose_publish_hz)
    if args.start_alignment and node.live_alignment_available and not args.fake_pose:
        node.start_live_alignment()
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    spin_thread = threading.Thread(target=executor.spin, daemon=True, name="ros_executor")
    spin_thread.start()

    web_root = Path(args.web_root) if args.web_root else None
    server = WebDashboardServer(node, args.host, args.port, web_root, node.project_root)
    server.start()

    try:
        while rclpy.ok():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        executor.shutdown()
        node.shutdown_workers()
        node.destroy_node()
        with contextlib.suppress(Exception):
            rclpy.shutdown()
        spin_thread.join(timeout=1.0)


if __name__ == "__main__":
    main()
