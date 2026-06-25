#!/usr/bin/env python3

import argparse
import asyncio
import contextlib
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Dict, List, Optional, Set, Tuple
from urllib.parse import quote

import numpy as np
import cv2
from aiohttp import web

try:
    import gi

    gi.require_version("Gst", "1.0")
    gi.require_version("GstSdp", "1.0")
    gi.require_version("GstWebRTC", "1.0")
    from gi.repository import Gst, GstSdp, GstWebRTC
except Exception:  # pragma: no cover - WebRTC support is optional
    gi = None
    Gst = None
    GstSdp = None
    GstWebRTC = None

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
from post_processing import (
    RecordingManager,
    build_default_topics,
    list_rosbags,
    load_post_processing_config,
)
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
    avatar_rotation_deg_xyz: Tuple[float, float, float]


@dataclass
class CameraSpec:
    name: str
    namespace: str
    label: str
    topic: str
    camera_info_topic: str
    topic_type: str
    rotation_deg: int
    row: int
    column: int
    column_span: int
    row_span: int


@dataclass
class CameraFrame:
    data: bytes
    stamp_ns: int
    received_monotonic: float
    mime_type: str
    width: int
    height: int
    version: int


class WebRTCCameraSession:
    def __init__(self, session_id: str, camera_name: str, frame_provider, fps: int = 10) -> None:
        if Gst is None or GstSdp is None or GstWebRTC is None:
            raise RuntimeError("GStreamer WebRTC Python bindings are unavailable")
        Gst.init(None)
        self.session_id = session_id
        self.camera_name = camera_name
        self.frame_provider = frame_provider
        self.fps = max(1, int(fps))
        self.duration_ns = int(1_000_000_000 / self.fps)
        self.pipeline = None
        self.appsrc = None
        self.webrtc = None
        self.offer_sdp: Optional[str] = None
        self.local_candidates: List[Dict[str, object]] = []
        self._candidate_lock = threading.Lock()
        self._offer_event = threading.Event()
        self._error: Optional[str] = None
        self._running = False
        self._push_thread: Optional[threading.Thread] = None
        self._frame_index = 0
        self.created_monotonic = time.monotonic()

    def start(self) -> str:
        pipeline_description = (
            "appsrc name=src is-live=true block=false format=time do-timestamp=false "
            f"caps=image/jpeg,framerate={self.fps}/1 "
            "! jpegdec "
            "! videoconvert "
            "! video/x-raw,format=I420 "
            "! queue max-size-buffers=2 leaky=downstream "
            "! vp8enc deadline=1 keyframe-max-dist=30 target-bitrate=800000 "
            "! rtpvp8pay pt=96 "
            "! application/x-rtp,media=video,encoding-name=VP8,payload=96 "
            "! webrtcbin name=webrtc bundle-policy=max-bundle"
        )
        self.pipeline = Gst.parse_launch(pipeline_description)
        self.appsrc = self.pipeline.get_by_name("src")
        self.webrtc = self.pipeline.get_by_name("webrtc")
        if self.appsrc is None or self.webrtc is None:
            raise RuntimeError("Failed to create appsrc/webrtcbin pipeline elements")
        self.webrtc.connect("on-negotiation-needed", self._on_negotiation_needed)
        self.webrtc.connect("on-ice-candidate", self._on_ice_candidate)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        self._running = True
        self.pipeline.set_state(Gst.State.PLAYING)
        self._push_thread = threading.Thread(
            target=self._push_loop,
            daemon=True,
            name=f"webrtc_push_{self.camera_name}_{self.session_id}",
        )
        self._push_thread.start()
        if not self._offer_event.wait(timeout=5.0):
            self.stop()
            raise RuntimeError("Timed out creating WebRTC offer")
        if self._error:
            self.stop()
            raise RuntimeError(self._error)
        if not self.offer_sdp:
            self.stop()
            raise RuntimeError("WebRTC offer was empty")
        return self.offer_sdp

    def stop(self) -> None:
        self._running = False
        if self.appsrc is not None:
            with contextlib.suppress(Exception):
                self.appsrc.emit("end-of-stream")
        if self._push_thread and self._push_thread.is_alive():
            self._push_thread.join(timeout=1.0)
        if self.pipeline is not None:
            with contextlib.suppress(Exception):
                self.pipeline.set_state(Gst.State.NULL)

    def set_answer(self, sdp: str) -> None:
        result, message = GstSdp.SDPMessage.new()
        if result != GstSdp.SDPResult.OK:
            raise RuntimeError("Failed to allocate SDP message")
        parse_result = GstSdp.sdp_message_parse_buffer(bytes(sdp, "utf-8"), message)
        if parse_result != GstSdp.SDPResult.OK:
            raise ValueError("Invalid SDP answer")
        answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, message)
        promise = Gst.Promise.new()
        self.webrtc.emit("set-remote-description", answer, promise)
        promise.interrupt()

    def add_remote_candidate(self, candidate: str, sdp_mline_index: int) -> None:
        if self.webrtc is None:
            return
        self.webrtc.emit("add-ice-candidate", int(sdp_mline_index), str(candidate))

    def drain_local_candidates(self) -> List[Dict[str, object]]:
        with self._candidate_lock:
            candidates = list(self.local_candidates)
            self.local_candidates.clear()
        return candidates

    def _on_negotiation_needed(self, element) -> None:
        promise = Gst.Promise.new_with_change_func(self._on_offer_created, None)
        element.emit("create-offer", None, promise)

    def _on_offer_created(self, promise, _unused) -> None:
        reply = promise.get_reply()
        offer = reply.get_value("offer") if reply is not None else None
        if offer is None:
            self._error = "create-offer returned no offer"
            self._offer_event.set()
            return
        local_promise = Gst.Promise.new()
        self.webrtc.emit("set-local-description", offer, local_promise)
        local_promise.interrupt()
        self.offer_sdp = offer.sdp.as_text()
        self._offer_event.set()

    def _on_ice_candidate(self, _element, mline_index: int, candidate: str) -> None:
        with self._candidate_lock:
            self.local_candidates.append(
                {
                    "sdpMLineIndex": int(mline_index),
                    "candidate": str(candidate),
                }
            )

    def _on_bus_message(self, _bus, message) -> None:
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            self._error = f"{error.message} | {debug or ''}"
            self._offer_event.set()

    def _push_loop(self) -> None:
        last_stamp_ns = -1
        interval = 1.0 / self.fps
        while self._running:
            frame = self.frame_provider(self.camera_name)
            if frame is not None and frame.data and frame.stamp_ns != last_stamp_ns:
                self._push_frame(frame)
                last_stamp_ns = frame.stamp_ns
            time.sleep(interval)

    def _push_frame(self, frame: CameraFrame) -> None:
        if self.appsrc is None:
            return
        buffer = Gst.Buffer.new_allocate(None, len(frame.data), None)
        buffer.fill(0, frame.data)
        pts = self._frame_index * self.duration_ns
        buffer.pts = pts
        buffer.dts = pts
        buffer.duration = self.duration_ns
        self._frame_index += 1
        result = self.appsrc.emit("push-buffer", buffer)
        if result != Gst.FlowReturn.OK:
            self._error = f"appsrc push-buffer failed: {result.value_nick}"


class PoseBridgeNode(LiveAlignmentMixin, Node):
    def __init__(
        self,
        config_path: Path,
        fake_pose: bool = False,
        pose_publish_hz: float = 30.0,
        enable_alignment_stream: bool = False,
    ) -> None:
        if rclpy is None:
            raise RuntimeError("rclpy is required to run the web dashboard backend")
        super().__init__("insight_multi_camera_dashboard_web")
        self.config_path = config_path
        self.fake_pose = bool(fake_pose)
        self.pose_publish_hz = max(1.0, float(pose_publish_hz))
        self.enable_alignment_stream = bool(enable_alignment_stream)
        self.max_points = 300
        self.pose_timeout_sec = 0.5

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
                namespace=enabled_camera_map[item["name"]]["namespace"],
                label=item.get("label", item["name"]),
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
                avatar_rotation_deg_xyz=tuple(float(value) for value in item.get("avatar_rotation_deg_xyz", [0.0, 0.0, 0.0])),
            )
            for item in config.get("poses", [])
        ]
        if self.reference_camera is None and self.poses:
            self.reference_camera = self.poses[0].name

        self.raw_traces: Dict[str, List[Tuple[float, float, float]]] = {pose.name: [] for pose in self.poses}
        self.latest_pose: Dict[str, Optional[Tuple[float, float, float]]] = {pose.name: None for pose in self.poses}
        self.latest_pose_sample: Dict[str, Optional[PoseSample]] = {pose.name: None for pose in self.poses}
        self.last_pose_received_time: Dict[str, float] = {pose.name: 0.0 for pose in self.poses}
        self.pose_history: Dict[str, Deque[PoseSample]] = {pose.name: deque(maxlen=160) for pose in self.poses}
        self.pose_history_lock = threading.Lock()
        self.pose_lock = threading.Lock()
        self.camera_frame_lock = threading.Lock()
        self.latest_camera_frames: Dict[str, Optional[CameraFrame]] = {camera.name: None for camera in self.cameras}
        self.camera_frame_versions: Dict[str, int] = {camera.name: 0 for camera in self.cameras}
        self.camera_frame_times: Dict[str, Deque[float]] = {camera.name: deque(maxlen=90) for camera in self.cameras}
        self.live_alignment_image_lock = threading.Lock()
        self.live_alignment_solution_lock = threading.Lock()
        self.ros_callback_group = ReentrantCallbackGroup()
        self.dashboard_subscriptions = []
        self._initialize_live_alignment_state()
        if self.world_to_reference:
            self.get_logger().info("Loaded persisted live alignment state for web dashboard startup")

        if self.fake_pose:
            self.create_timer(1.0 / self.pose_publish_hz, self._update_fake_pose, callback_group=self.ros_callback_group)
            self.get_logger().info("Running in fake-pose demo mode")
        else:
            self._create_pose_subscriptions()
            self._create_dashboard_image_subscriptions()
            if self.live_alignment_available:
                self._create_alignment_subscriptions()

        if self.live_alignment_available:
            self.live_alignment_timer = self.create_timer(
                1.0 / max(self.live_alignment_processing_hz, 0.5),
                self._process_live_alignment,
                callback_group=self.ros_callback_group,
            )
            self._set_live_alignment_timer_enabled(False)

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

    def _create_dashboard_image_subscriptions(self) -> None:
        image_qos = make_image_qos(reliability=self.image_qos_reliability)
        for camera in self.cameras:
            msg_type = CompressedImage if camera.topic_type == "compressed" else RosImage
            sub = self.create_subscription(
                msg_type,
                camera.topic,
                self._make_dashboard_image_callback(camera.name, camera.topic_type),
                image_qos,
                callback_group=self.ros_callback_group,
            )
            self.dashboard_subscriptions.append(sub)
            self.get_logger().info(f"Images: {camera.name} <- {camera.topic} type={camera.topic_type}")

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

    def _make_dashboard_image_callback(self, camera_name: str, topic_type: str):
        def callback(msg) -> None:
            frame = self._encode_dashboard_frame(camera_name, topic_type, msg)
            if frame is None:
                return
            with self.camera_frame_lock:
                self.latest_camera_frames[camera_name] = frame
                self.camera_frame_times[camera_name].append(frame.received_monotonic)

        return callback

    def _encode_dashboard_frame(self, camera_name: str, topic_type: str, msg: object) -> Optional[CameraFrame]:
        stamp_ns = self._stamp_to_ns(msg.header.stamp)
        received_monotonic = time.monotonic()
        with self.camera_frame_lock:
            version = self.camera_frame_versions.get(camera_name, 0) + 1
            self.camera_frame_versions[camera_name] = version
        if topic_type == "compressed":
            data = bytes(msg.data)
            width, height = self._jpeg_dimensions(data)
            return CameraFrame(
                data=data,
                stamp_ns=stamp_ns,
                received_monotonic=received_monotonic,
                mime_type="image/jpeg",
                width=width,
                height=height,
                version=version,
            )
        image = self._decode_calibration_message(topic_type, msg)
        if image is None:
            return None
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

    def _record_pose_sample(self, pose_name: str, pose_sample: PoseSample) -> None:
        with self.pose_history_lock:
            self.pose_history[pose_name].append(pose_sample)
        with self.pose_lock:
            self.latest_pose_sample[pose_name] = pose_sample
            self.latest_pose[pose_name] = self._transform_pose_point(pose_name, pose_sample.position)
            self.last_pose_received_time[pose_name] = time.monotonic()
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
                        "trace": [
                            [float(sample[0]), float(sample[1]), float(sample[2])]
                            for sample in self.transformed_trace(pose.name)
                        ],
                        "avatar_model": pose.avatar_model,
                        "avatar_scale": pose.avatar_scale,
                        "avatar_rotation_deg_xyz": [float(value) for value in pose.avatar_rotation_deg_xyz],
                    }
                )
        return {
            "type": "pose_update",
            "timestamp_ms": int(time.time() * 1000),
            "fake_pose": self.fake_pose,
            "alignment": self.build_alignment_payload(),
            "poses": poses,
        }

    def build_alignment_payload(self) -> Dict[str, object]:
        target_camera = getattr(self, "live_alignment_target_camera", None)
        inlier_counts = getattr(self, "live_alignment_inlier_counts", {})
        return {
            "available": bool(self.live_alignment_available and not self.fake_pose),
            "active": bool(self.live_alignment_active),
            "status_text": self.alignment_status_text(),
            "lock_on_first_solution": bool(self.live_alignment_lock_on_first_solution),
            "required_samples": int(self.live_alignment_required_samples),
            "visible_cameras": int(getattr(self, "live_alignment_visible_cameras", 0)),
            "camera_count": len(self.cameras),
            "inlier_count": int(0 if target_camera is None else inlier_counts.get(target_camera, 0)),
            "last_status": str(getattr(self, "live_alignment_last_status", "")),
            "has_solution": bool(self.world_to_reference),
        }

    def build_camera_payload(self) -> Dict[str, object]:
        now = time.monotonic()
        cameras = []
        with self.camera_frame_lock:
            for camera in self.cameras:
                frame = self.latest_camera_frames.get(camera.name)
                frame_times = list(self.camera_frame_times.get(camera.name, []))
                recent_times = [item for item in frame_times if now - item <= 2.0]
                fps = 0.0
                if len(recent_times) >= 2:
                    span = max(recent_times[-1] - recent_times[0], 1e-6)
                    fps = (len(recent_times) - 1) / span
                stale = frame is None or (now - frame.received_monotonic) > 1.0
                cameras.append(
                    {
                        "name": camera.name,
                        "label": camera.label,
                        "topic": camera.topic,
                        "type": camera.topic_type,
                        "visible": frame is not None,
                        "stale": stale,
                        "stamp_ns": 0 if frame is None else frame.stamp_ns,
                        "age_ms": None if frame is None else (now - frame.received_monotonic) * 1000.0,
                        "fps": fps,
                        "width": 0 if frame is None else frame.width,
                        "height": 0 if frame is None else frame.height,
                        "version": 0 if frame is None else frame.version,
                        "frame_url": f"/api/cameras/{quote(camera.name, safe='')}/frame",
                        "rotation_deg": camera.rotation_deg,
                        "row": camera.row,
                        "column": camera.column,
                        "row_span": camera.row_span,
                        "column_span": camera.column_span,
                    }
                )
        return {
            "type": "camera_update",
            "timestamp_ms": int(time.time() * 1000),
            "cameras": cameras,
        }

    def latest_camera_frame(self, camera_name: str) -> Optional[CameraFrame]:
        with self.camera_frame_lock:
            return self.latest_camera_frames.get(camera_name)

    def model_asset_url(self, avatar_model: Optional[str]) -> Optional[str]:
        if not avatar_model:
            return None
        return f"/asset?path={quote(avatar_model, safe='')}"

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
        recording_manager: RecordingManager,
        results_root: Path,
    ) -> None:
        self.node = node
        self.host = host
        self.port = int(port)
        self.web_root = web_root.resolve() if web_root else None
        self.project_root = project_root.resolve()
        self.recording_manager = recording_manager
        self.results_root = results_root.resolve()
        self._clients: Set[web.WebSocketResponse] = set()
        self._webrtc_sessions: Dict[str, WebRTCCameraSession] = {}
        self._webrtc_session_lock = threading.Lock()
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
        app = web.Application(middlewares=[self._json_error_middleware])
        app.router.add_get("/ws", self._handle_ws)
        app.router.add_get("/healthz", self._handle_healthz)
        app.router.add_get("/api/poses", self._handle_pose_snapshot)
        app.router.add_get("/api/alignment", self._handle_alignment_snapshot)
        app.router.add_post("/api/alignment/start", self._handle_alignment_start)
        app.router.add_post("/api/alignment/stop", self._handle_alignment_stop)
        app.router.add_get("/api/cameras", self._handle_camera_snapshot)
        app.router.add_get("/api/cameras/{camera_name}/frame", self._handle_camera_frame)
        app.router.add_get("/api/images/capabilities", self._handle_image_capabilities)
        app.router.add_post("/api/images/webrtc/probe", self._handle_image_webrtc_probe)
        app.router.add_post("/api/images/webrtc/{camera_name}/offer", self._handle_image_webrtc_offer)
        app.router.add_post("/api/images/webrtc/{session_id}/answer", self._handle_image_webrtc_answer)
        app.router.add_post("/api/images/webrtc/{session_id}/candidate", self._handle_image_webrtc_candidate)
        app.router.add_get("/api/images/webrtc/{session_id}/candidates", self._handle_image_webrtc_candidates)
        app.router.add_delete("/api/images/webrtc/{session_id}", self._handle_image_webrtc_stop)
        app.router.add_get("/api/recording/status", self._handle_recording_status)
        app.router.add_get("/api/recording/topics", self._handle_recording_topics)
        app.router.add_post("/api/recording/start", self._handle_recording_start)
        app.router.add_post("/api/recording/stop", self._handle_recording_stop)
        app.router.add_post("/api/recording/sync", self._handle_recording_sync)
        app.router.add_get("/api/rosbags", self._handle_rosbag_list)
        app.router.add_get("/asset", self._handle_asset)
        if self.web_root and self.web_root.exists():
            app.router.add_get("/", self._handle_index)
            app.router.add_get("/3d", self._handle_index)
            app.router.add_get("/cameras", self._handle_cameras_page)
            app.router.add_get("/images", self._handle_images_page)
            app.router.add_get("/bags", self._handle_bags_page)
            app.router.add_get("/recording", self._handle_recording_page)
            app.router.add_get("/scoring", self._handle_scoring_page)
            app.router.add_get("/optimization", self._handle_optimization_page)
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
        self._stop_all_webrtc_sessions()
        self.recording_manager.stop()

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

    @web.middleware
    async def _json_error_middleware(self, request: web.Request, handler):
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except RuntimeError as exc:
            return web.json_response({"error": str(exc)}, status=409)
        except Exception as exc:
            self.node.get_logger().error(f"Unhandled web error for {request.path}: {exc}")
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_alignment_snapshot(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "type": "alignment_status",
                "alignment": self.node.build_alignment_payload(),
            }
        )

    async def _handle_alignment_start(self, _request: web.Request) -> web.Response:
        status_text = self.node.start_live_alignment()
        return web.json_response(
            {
                "ok": bool(self.node.live_alignment_active),
                "type": "alignment_status",
                "status_text": status_text,
                "alignment": self.node.build_alignment_payload(),
            }
        )

    async def _handle_alignment_stop(self, _request: web.Request) -> web.Response:
        status_text = self.node.stop_live_alignment()
        return web.json_response(
            {
                "ok": True,
                "type": "alignment_status",
                "status_text": status_text,
                "alignment": self.node.build_alignment_payload(),
            }
        )

    async def _handle_camera_snapshot(self, _request: web.Request) -> web.Response:
        return web.json_response(self.node.build_camera_payload())

    async def _handle_camera_frame(self, request: web.Request) -> web.Response:
        camera_name = request.match_info.get("camera_name", "")
        frame = self.node.latest_camera_frame(camera_name)
        if frame is None:
            raise web.HTTPNotFound(text="camera frame not available yet")
        headers = {
            "Cache-Control": "no-store, max-age=0",
            "X-Frame-Stamp-Ns": str(frame.stamp_ns),
            "X-Frame-Version": str(frame.version),
        }
        return web.Response(body=frame.data, content_type=frame.mime_type, headers=headers)

    async def _handle_image_capabilities(self, _request: web.Request) -> web.Response:
        return web.json_response(self._build_image_capabilities())

    async def _handle_image_webrtc_probe(self, request: web.Request) -> web.Response:
        payload = {}
        if request.can_read_body:
            try:
                payload = await request.json()
            except json.JSONDecodeError:
                payload = {}
        codec = str(payload.get("codec", "vp8") if isinstance(payload, dict) else "vp8").lower()
        if codec not in {"vp8", "h264"}:
            raise web.HTTPBadRequest(text="codec must be vp8 or h264")
        script_path = Path(__file__).resolve().parent / "probe_webrtc_pipeline.py"

        def run_probe() -> subprocess.CompletedProcess:
            return subprocess.run(
                [sys.executable, str(script_path), "--codec", codec],
                cwd=str(self.project_root),
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=8.0,
            )

        try:
            result = await asyncio.to_thread(run_probe)
        except subprocess.TimeoutExpired:
            return web.json_response(
                {"ok": False, "codec": codec, "stdout": "", "stderr": "WebRTC probe timed out"},
                status=504,
            )
        return web.json_response(
            {
                "ok": result.returncode == 0,
                "codec": codec,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
            status=200 if result.returncode == 0 else 409,
        )

    async def _handle_image_webrtc_offer(self, request: web.Request) -> web.Response:
        camera_name = request.match_info.get("camera_name", "")
        known_cameras = {camera.name for camera in self.node.cameras}
        if camera_name not in known_cameras:
            raise web.HTTPNotFound(text="unknown camera")
        if self.node.latest_camera_frame(camera_name) is None:
            raise web.HTTPConflict(text="camera frame not available yet")
        session_id = f"{camera_name}_{int(time.time() * 1000)}"
        session = WebRTCCameraSession(
            session_id=session_id,
            camera_name=camera_name,
            frame_provider=self.node.latest_camera_frame,
            fps=10,
        )
        self._stop_webrtc_sessions_for_camera(camera_name)
        try:
            offer_sdp = await asyncio.to_thread(session.start)
        except Exception:
            session.stop()
            raise
        with self._webrtc_session_lock:
            self._webrtc_sessions[session_id] = session
        return web.json_response(
            {
                "ok": True,
                "session_id": session_id,
                "camera_name": camera_name,
                "codec": "vp8",
                "offer": {
                    "type": "offer",
                    "sdp": offer_sdp,
                },
            }
        )

    async def _handle_image_webrtc_answer(self, request: web.Request) -> web.Response:
        session = self._get_webrtc_session(request.match_info.get("session_id", ""))
        payload = await request.json()
        answer = payload.get("answer") if isinstance(payload, dict) else None
        sdp = answer.get("sdp") if isinstance(answer, dict) else None
        if not sdp:
            raise web.HTTPBadRequest(text="missing answer.sdp")
        await asyncio.to_thread(session.set_answer, sdp)
        return web.json_response({"ok": True, "session_id": session.session_id})

    async def _handle_image_webrtc_candidate(self, request: web.Request) -> web.Response:
        session = self._get_webrtc_session(request.match_info.get("session_id", ""))
        payload = await request.json()
        candidate = payload.get("candidate") if isinstance(payload, dict) else None
        if not isinstance(candidate, dict):
            raise web.HTTPBadRequest(text="missing candidate")
        candidate_text = candidate.get("candidate")
        if not candidate_text:
            return web.json_response({"ok": True, "ignored": True})
        session.add_remote_candidate(
            candidate=str(candidate_text),
            sdp_mline_index=int(candidate.get("sdpMLineIndex", 0)),
        )
        return web.json_response({"ok": True, "session_id": session.session_id})

    async def _handle_image_webrtc_candidates(self, request: web.Request) -> web.Response:
        session = self._get_webrtc_session(request.match_info.get("session_id", ""))
        return web.json_response(
            {
                "ok": True,
                "session_id": session.session_id,
                "candidates": session.drain_local_candidates(),
            }
        )

    async def _handle_image_webrtc_stop(self, request: web.Request) -> web.Response:
        session_id = request.match_info.get("session_id", "")
        stopped = self._stop_webrtc_session(session_id)
        return web.json_response({"ok": True, "session_id": session_id, "stopped": stopped})

    def _get_webrtc_session(self, session_id: str) -> WebRTCCameraSession:
        with self._webrtc_session_lock:
            session = self._webrtc_sessions.get(session_id)
        if session is None:
            raise web.HTTPNotFound(text="WebRTC session not found")
        return session

    def _stop_webrtc_session(self, session_id: str) -> bool:
        with self._webrtc_session_lock:
            session = self._webrtc_sessions.pop(session_id, None)
        if session is None:
            return False
        session.stop()
        return True

    def _stop_webrtc_sessions_for_camera(self, camera_name: str) -> None:
        with self._webrtc_session_lock:
            session_ids = [
                session_id
                for session_id, session in self._webrtc_sessions.items()
                if session.camera_name == camera_name
            ]
        for session_id in session_ids:
            self._stop_webrtc_session(session_id)

    def _stop_all_webrtc_sessions(self) -> None:
        with self._webrtc_session_lock:
            session_ids = list(self._webrtc_sessions)
        for session_id in session_ids:
            self._stop_webrtc_session(session_id)

    def _build_image_capabilities(self) -> Dict[str, object]:
        elements = self._detect_gstreamer_elements(
            [
                "webrtcbin",
                "nice",
                "nvv4l2h264enc",
                "nvv4l2h265enc",
                "nvv4l2decoder",
                "nvjpegdec",
                "nvvidconv",
                "openh264enc",
                "x264enc",
                "vp8enc",
            ]
        )
        has_webrtc = bool(elements.get("webrtcbin") and elements.get("nice"))
        hardware_encoder = None
        if elements.get("nvv4l2h264enc"):
            hardware_encoder = "nvv4l2h264enc"
        elif elements.get("nvv4l2h265enc"):
            hardware_encoder = "nvv4l2h265enc"
        software_encoder = None
        for candidate in ("openh264enc", "x264enc", "vp8enc"):
            if elements.get(candidate):
                software_encoder = candidate
                break
        active_path = "webrtc-hardware-h264" if hardware_encoder else "jpeg-preview"
        if has_webrtc and not hardware_encoder and software_encoder:
            active_path = "webrtc-software-low-latency-planned"
        notes = []
        if hardware_encoder:
            notes.append(f"Hardware encoder available: {hardware_encoder}.")
        else:
            notes.append("No Jetson hardware H.264/H.265 encoder detected on this device.")
        if elements.get("nvjpegdec") or elements.get("nvvidconv"):
            notes.append("NVIDIA decode/convert elements are available for the pre-encode path.")
        if has_webrtc:
            notes.append("WebRTC transport dependencies are present.")
        else:
            notes.append("WebRTC transport is incomplete; install gstreamer1.0-nice if nice is missing.")
        return {
            "type": "image_capabilities",
            "gstreamer": {
                "available": shutil.which("gst-inspect-1.0") is not None,
                "elements": elements,
            },
            "webrtc_ready": has_webrtc,
            "hardware_encoder": hardware_encoder,
            "software_encoder": software_encoder,
            "decode_acceleration": {
                "nvjpegdec": bool(elements.get("nvjpegdec")),
                "nvv4l2decoder": bool(elements.get("nvv4l2decoder")),
                "nvvidconv": bool(elements.get("nvvidconv")),
            },
            "active_path": active_path,
            "cameras": [
                {
                    "name": camera.name,
                    "label": camera.label,
                    "topic": camera.topic,
                    "type": camera.topic_type,
                }
                for camera in self.node.cameras
            ],
            "notes": notes,
        }

    @staticmethod
    def _detect_gstreamer_elements(elements: List[str]) -> Dict[str, bool]:
        if shutil.which("gst-inspect-1.0") is None:
            return {element: False for element in elements}
        detected = {}
        for element in elements:
            try:
                result = subprocess.run(
                    ["gst-inspect-1.0", element],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2.0,
                )
                detected[element] = result.returncode == 0
            except Exception:
                detected[element] = False
        return detected

    async def _handle_recording_status(self, _request: web.Request) -> web.Response:
        return web.json_response(self.recording_manager.status())

    async def _handle_recording_topics(self, _request: web.Request) -> web.Response:
        return web.json_response(self.recording_manager.current_topic_catalog(refresh=True))

    async def _handle_recording_start(self, request: web.Request) -> web.Response:
        payload = {}
        if request.can_read_body:
            try:
                payload = await request.json()
            except json.JSONDecodeError as exc:
                return web.json_response({"error": f"Invalid JSON body: {exc}"}, status=400)
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            return web.json_response({"error": "Request body must be a JSON object."}, status=400)
        topics = payload.get("topics")
        if "topics" in payload and not isinstance(topics, list):
            return web.json_response({"error": "Field 'topics' must be a list."}, status=400)
        status = self.recording_manager.start(topics=topics)
        return web.json_response(status)

    async def _handle_recording_stop(self, _request: web.Request) -> web.Response:
        return web.json_response(self.recording_manager.stop())

    async def _handle_recording_sync(self, _request: web.Request) -> web.Response:
        sync_status = self.recording_manager.sync_recording_to_host()
        payload = self.recording_manager.status()
        payload["sync_status"] = sync_status
        return web.json_response(payload)

    async def _handle_rosbag_list(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "type": "rosbag_list",
                "rosbag_root": str(self.recording_manager.rosbag_root),
                "results_root": str(self.results_root),
                "bags": list_rosbags(self.recording_manager.rosbag_root, self.results_root),
            }
        )

    async def _handle_index(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.web_root / "3d.html")

    async def _handle_recording_page(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.web_root / "recording.html")

    async def _handle_cameras_page(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.web_root / "cameras.html")

    async def _handle_images_page(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.web_root / "images.html")

    async def _handle_bags_page(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.web_root / "bags.html")

    async def _handle_scoring_page(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.web_root / "scoring.html")

    async def _handle_optimization_page(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.web_root / "optimization.html")

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
    parser.add_argument("--view-mode", choices=("3d",), default="3d")
    parser.add_argument("--fake-pose", action="store_true")
    parser.add_argument("--pose-publish-hz", type=float, default=20.0)
    parser.add_argument("--start-alignment", action="store_true")
    parser.add_argument("--post-processing-config", default=str(Path(__file__).resolve().parents[1] / "config" / "post_processing.json"))
    parser.add_argument("--rosbag-dir", "-rosbag-dir", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config_path = Path(args.config).resolve()
    raw_config = load_setup(config_path)
    project_root = config_path.resolve().parents[1]
    post_processing_config = load_post_processing_config(Path(args.post_processing_config).resolve())
    ros_domain_id = int(raw_config.get("ros_domain_id", 10))
    if rclpy is None:
        raise RuntimeError("rclpy is not available in this environment")

    os.environ.setdefault("ROS_DOMAIN_ID", str(ros_domain_id))
    rosbag_dir_value = (
        args.rosbag_dir
        or os.environ.get("INSIGHT_ROSBAG_DIR")
        or post_processing_config.get("rosbag_dir")
        or "rosbags"
    )
    host_rosbag_sync_value = (
        os.environ.get("INSIGHT_HOST_ROSBAG_SYNC_DIR")
        or post_processing_config.get("host_rosbag_sync_dir")
        or ""
    )
    host_rosbag_sync_ssh_target = (
        os.environ.get("INSIGHT_HOST_ROSBAG_SYNC_SSH_TARGET")
        or post_processing_config.get("host_rosbag_sync_ssh_target")
        or ""
    )
    results_dir_value = post_processing_config.get("results_dir", "outputs/results")
    rosbag_root = Path(rosbag_dir_value)
    if not rosbag_root.is_absolute():
        rosbag_root = (project_root / rosbag_root).resolve()
    host_rosbag_sync_root: Optional[Path] = None
    if str(host_rosbag_sync_value).strip():
        host_rosbag_sync_root = Path(str(host_rosbag_sync_value).strip())
        if not host_rosbag_sync_root.is_absolute():
            host_rosbag_sync_root = (project_root / host_rosbag_sync_root).resolve()
    results_root = Path(results_dir_value)
    if not results_root.is_absolute():
        results_root = (project_root / results_root).resolve()
    rosbag_root.mkdir(parents=True, exist_ok=True)
    results_root.mkdir(parents=True, exist_ok=True)

    configured_record_topics = post_processing_config.get("record_topics") or []
    default_record_topics = configured_record_topics if configured_record_topics else build_default_topics(raw_config)
    recording_manager = RecordingManager(
        raw_config=raw_config,
        ros_domain_id=ros_domain_id,
        rosbag_root=rosbag_root,
        max_cache_size=int(post_processing_config.get("max_cache_size", 2147483648)),
        default_topics=default_record_topics,
        host_sync_dir=host_rosbag_sync_root,
        host_sync_ssh_target=str(host_rosbag_sync_ssh_target or "").strip(),
        sync_to_host_on_stop=bool(post_processing_config.get("sync_rosbag_to_host", False)),
        publisher_checker=None,
    )

    rclpy.init(args=None)
    enable_alignment_stream = not args.fake_pose
    node = PoseBridgeNode(
        config_path,
        fake_pose=args.fake_pose,
        pose_publish_hz=args.pose_publish_hz,
        enable_alignment_stream=enable_alignment_stream,
    )
    node.get_logger().info(f"View mode={args.view_mode} alignment_stream={enable_alignment_stream}")
    if args.start_alignment and node.live_alignment_available and not args.fake_pose:
        node.start_live_alignment()
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    spin_thread = threading.Thread(target=executor.spin, daemon=True, name="ros_executor")
    spin_thread.start()

    web_root = Path(args.web_root) if args.web_root else None
    server = WebDashboardServer(node, args.host, args.port, web_root, node.project_root, recording_manager, results_root)
    server.start()

    try:
        while rclpy.ok():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        executor.shutdown()
        node.destroy_node()
        with contextlib.suppress(Exception):
            rclpy.shutdown()
        spin_thread.join(timeout=1.0)


if __name__ == "__main__":
    main()
