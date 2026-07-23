#!/usr/bin/env python3

import argparse
import asyncio
import contextlib
import fcntl
import http.client
import json
import math
import os
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass
from multiprocessing.connection import Client
from pathlib import Path
from typing import Deque, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import quote

import numpy as np
import cv2
from aiohttp import web

try:
    import rclpy
    from geometry_msgs.msg import PoseStamped
    from rclpy.callback_groups import ReentrantCallbackGroup
    from rclpy.executors import MultiThreadedExecutor
    from rclpy.node import Node
    from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
    from rclpy.qos_event import SubscriptionEventCallbacks
    from sensor_msgs.msg import CameraInfo
    from sensor_msgs.msg import CompressedImage, Image as RosImage
    from vision_msgs.msg import Detection2DArray
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
    SubscriptionEventCallbacks = None
    CameraInfo = None
    CompressedImage = None
    RosImage = None
    Detection2DArray = None

from camera_setup import (
    AVAILABLE_AVATAR_MODELS,
    IMAGE_STREAMS,
    avatar_model_defaults,
    build_dashboard_config,
    camera_info_topic,
    image_topic,
    load_setup,
)
from check_bag import analyze_bag, nominal_for
from gripper_tracking import GripperTrackingMixin
from hand_overlay import HandOverlayMixin
from hw_jpeg import HwJpegCodec
from inprocess_bag_writer import InProcessBagWriter
import perf_tracker
from perf_tracker import track
from live_alignment import LiveAlignmentMixin
from post_processing import (
    OptimizationManager,
    PlaybackManager,
    RecordingManager,
    STORAGE_CONFIG_PATH,
    build_default_topics,
    list_rosbags,
    load_post_processing_config,
)
from session_alignment import PoseSample
from dashboard_web import WebDashboardServer, bagplay_topic

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
    avatar_offset_xyz: Tuple[float, float, float]


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
    alignment_image_stream: Optional[str] = None


@dataclass
class CameraFrame:
    data: bytes
    stamp_ns: int
    received_monotonic: float
    mime_type: str
    width: int
    height: int
    version: int
    hand_overlay_pending: bool = False


# The polling fallback only needs a recent still frame to cover a WebRTC
# disconnect/reconnect. Encoding it at every source frame wastes the same
# NVJPEG/GIL time that WebRTC was introduced to avoid.
WEBRTC_JPEG_FALLBACK_INTERVAL_SEC = 0.5
# Recording owns the source frames.  Limit only the visual WebRTC preview
# while it is active, so JPEG copies/IPC cannot starve the DDS callbacks that
# feed the bag writer.  The preview returns to native rate immediately after
# stop; the bag itself continues to receive every source frame.
RECORDING_WEBRTC_PREVIEW_FPS = 10.0


class PoseBridgeNode(LiveAlignmentMixin, GripperTrackingMixin, HandOverlayMixin, Node):
    def __init__(
        self,
        config_path: Path,
        fake_pose: bool = False,
        pose_publish_hz: float = 50.0,
        enable_alignment_stream: bool = False,
        webrtc_port: int = 8766,
    ) -> None:
        if rclpy is None:
            raise RuntimeError("rclpy is required to run the web dashboard backend")
        super().__init__("insight_multi_camera_dashboard_web")
        self.config_path = config_path
        self.fake_pose = bool(fake_pose)
        self.enable_alignment_stream = bool(enable_alignment_stream)
        self.max_points = 300

        raw_config = load_setup(config_path)
        config = build_dashboard_config(raw_config)
        enabled_camera_map = {
            camera["name"]: camera for camera in raw_config.get("cameras", []) if camera.get("enabled", True)
        }
        self.project_root = config_path.resolve().parents[1]
        self.window_title = config.get("window_title", "Insight Web Dashboard")
        trajectory_config = config.get("trajectory", {})
        # config/cameras.json sets this to "reliable" on this fleet: measured
        # 2026-07-13 that "best_effort" silently drops a whole image sample
        # on a single lost UDP fragment (no retransmission), causing ~1-4%
        # scattered single-frame loss in recordings with 0 kernel-level
        # errors to show for it -- "reliable" measured 0.0% loss across
        # repeated trials with no regression to the other topics.
        self.image_qos_reliability = str(trajectory_config.get("image_qos_reliability", "best_effort"))
        self.pose_publish_hz = max(1.0, float(trajectory_config.get("pose_publish_hz", pose_publish_hz)))
        self.pose_timeout_sec = max(0.2, float(trajectory_config.get("pose_timeout_sec", 2.0)))
        self.camera_stale_timeout_sec = max(0.2, float(trajectory_config.get("camera_stale_timeout_sec", 2.0)))
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
                alignment_image_stream=item.get("alignment_image_stream") or None,
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
                avatar_offset_xyz=tuple(float(value) for value in item.get("avatar_offset_xyz", [0.0, 0.0, 0.0])),
            )
            for item in config.get("poses", [])
        ]
        if self.reference_camera is None and self.poses:
            self.reference_camera = self.poses[0].name

        # Set by main() once RecordingManager exists (constructed after this
        # node) -- consulted by the stale-participant watchdog so it doesn't
        # kill an in-progress recording over one camera dropping out.
        self.recording_manager: Optional[RecordingManager] = None
        self._playback_mode: bool = False
        # Settings toggle: render poses as large role-colored dots instead of
        # loading the GLB avatar models -- the clean stick-figure look for the
        # skeleton overlays. In-memory only, like the rest of Settings.
        self.stick_figure_mode: bool = False
        # Bounded deque: append is O(1) and old points fall off automatically.
        # A plain list needed an O(max_points) slice-delete per pose message,
        # which at 100Hz x 3 poses was ~300 full-list shifts per second.
        self.raw_traces: Dict[str, Deque[Tuple[float, float, float]]] = {
            pose.name: deque(maxlen=self.max_points) for pose in self.poses
        }
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
        # Humble creates one default "incompatible QoS" waitable for every
        # subscription. On this Jetson/rmw_fastrtps combination the rclpy
        # executor has repeatedly segfaulted while enumerating those
        # QoSEventHandler objects (qos_event.py get_num_entities/is_ready/
        # __enter__), especially while rosbag recorder participants are
        # leaving during stop/merge. We do not consume QoS event callbacks;
        # the explicit profiles below are fixed and validated at startup.
        # Disable only the unused default handlers so normal subscription
        # data callbacks remain unchanged and the crashing waitables are not
        # added to the executor at all.
        self.subscription_event_callbacks = SubscriptionEventCallbacks(
            use_default_callbacks=False
        )
        self.dashboard_subscriptions = []
        # Recording feeds off this node's own image subscriptions instead of
        # a second `ros2 bag record` reader -- see inprocess_bag_writer.py
        # for why a second reader on the same image topic causes drops.
        # Keyed by output path (one writer/thread per camera, see
        # start_image_recording) so cameras don't serialize behind each
        # other on a single writer thread.
        self._recording_writers: Dict[str, InProcessBagWriter] = {}
        self._recording_writer_by_topic: Dict[str, InProcessBagWriter] = {}
        # Camera header stamps are monotonic in the camera's boot-relative
        # clock, while rosbag2 requires epoch-ish timestamps in the shared
        # recording timeline.  Each recording establishes one fixed mapping
        # per image topic at its first frame; later frames must retain the
        # camera cadence rather than inheriting Python executor jitter.
        self._recording_timestamp_offsets_ns: Dict[str, Optional[int]] = {}
        self._recording_header_audit: Dict[str, Dict[str, object]] = {}
        self._recording_writer_lock = threading.Lock()
        # Latest-frame handoff from the (near-zero-cost) image subscription
        # callbacks to the per-camera worker threads that do the heavy
        # per-frame work (gripper detect, alignment, display encode). Plain
        # dict item set/pop is atomic under the GIL -- no lock needed for a
        # single-producer single-consumer latest-value slot.
        self._pending_frames: Dict[str, object] = {}
        self._pending_frame_events: Dict[str, threading.Event] = {
            camera.name: threading.Event() for camera in self.cameras
        }
        self._configure_gripper_tracking(str(self.project_root / "config" / "gripper_calibration.json"))
        self._configure_hand_overlay()
        # NVJPEG hardware encode for raw NV12/mono8 display frames and the
        # hand-overlay decode/re-encode; None on non-Jetson hosts, and every
        # call site keeps its cv2 path as fallback (see hw_jpeg.py).
        self._hw_jpeg = HwJpegCodec.create(log=self.get_logger().info)
        # Hardware H.264 WebRTC (GStreamer/webrtcbin) runs in its own process
        # (webrtc_worker.py) now, not in this one -- see wiki changelog
        # 2026-07-22: with the 3D pose broadcast and all 3 WebRTC sessions
        # active together (the real "3 camera panels + 3D on one screen"
        # case), the two were measured fighting over this process's GIL with
        # idle CPU sitting unused, dropping every camera's fps 12-30%. The
        # frontend keeps its polling fallback either way (webrtc_available
        # false when the worker isn't up/ready or lacks the hardware
        # elements -- e.g. the lite/lite-779 dev profiles).
        self.webrtc_port = webrtc_port
        self._webrtc_ipc_path = os.path.join(tempfile.gettempdir(), f"insight_webrtc_{os.getpid()}.sock")
        self._webrtc_authkey = secrets.token_bytes(32)
        self._webrtc_has_sessions: Dict[str, bool] = {}
        self._pending_webrtc_frames: Dict[str, Tuple[str, int, int, bytes]] = {}
        self._webrtc_frame_event = threading.Event()
        self._webrtc_available_cached = False
        self._last_webrtc_fallback_jpeg_at: Dict[str, float] = {}
        self._last_recording_preview_at: Dict[str, float] = {}
        self._webrtc_proc = self._start_webrtc_worker()
        threading.Thread(target=self._webrtc_ipc_loop, daemon=True, name="webrtc_ipc").start()
        threading.Thread(target=self._webrtc_healthz_loop, daemon=True, name="webrtc_healthz").start()
        # Hand-overlay JPEG compositing also runs in its own process now,
        # same reason and same shape as the WebRTC split above (see
        # hand_overlay.compose_hand_overlay_jpeg's docstring): its
        # hw_jpeg.py round trip shares this GStreamer/GIL contention but
        # only fires once a hand is actually detected.
        self._hand_overlay_ipc_path = os.path.join(tempfile.gettempdir(), f"insight_hand_overlay_{os.getpid()}.sock")
        self._hand_overlay_authkey = secrets.token_bytes(32)
        self._pending_hand_overlay_frames: Dict[str, Tuple[int, bytes, list]] = {}
        self._hand_overlay_frame_event = threading.Event()
        # Highest composited version actually applied per camera so far --
        # see _apply_composited_hand_overlay for why this replaces an exact
        # camera_frame_versions match.
        self._hand_overlay_last_applied: Dict[str, int] = {}
        self._hand_overlay_proc = self._start_hand_overlay_worker()
        threading.Thread(target=self._hand_overlay_ipc_loop, daemon=True, name="hand_overlay_ipc").start()
        self.create_timer(10.0, self._log_perf_summary, callback_group=self.ros_callback_group)
        self._initialize_live_alignment_state()
        if self.world_to_reference:
            self.get_logger().info("Loaded persisted live alignment state for web dashboard startup")

        if self.fake_pose:
            self.create_timer(1.0 / self.pose_publish_hz, self._update_fake_pose, callback_group=self.ros_callback_group)
            self.get_logger().info("Running in fake-pose demo mode")
        else:
            self._create_pose_subscriptions()
            self._create_dashboard_image_subscriptions()
            self._create_hand_overlay_subscriptions()
            if self.live_alignment_available:
                self._create_alignment_subscriptions()
            threading.Thread(
                target=self._stale_participant_watchdog_loop,
                daemon=True,
                name="stale_dds_watchdog",
            ).start()

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
                self._make_pose_callback(pose.name, is_live=True),
                pose_qos,
                callback_group=self.ros_callback_group,
                event_callbacks=self.subscription_event_callbacks,
            )
            self.dashboard_subscriptions.append(sub)
            # See _make_dashboard_image_callback: same live/playback topic
            # split so replayed trajectories never blend with a live one.
            playback_sub = self.create_subscription(
                PoseStamped,
                bagplay_topic(pose.topic),
                self._make_pose_callback(pose.name, is_live=False),
                pose_qos,
                callback_group=self.ros_callback_group,
                event_callbacks=self.subscription_event_callbacks,
            )
            self.dashboard_subscriptions.append(playback_sub)
            self.get_logger().info(f"Trajectory: {pose.name} <- {pose.topic}")

    def _alignment_stream_for(self, camera: "CameraSpec") -> str:
        return camera.alignment_image_stream or self.live_alignment_image_stream

    def _create_dashboard_image_subscriptions(self) -> None:
        # depth>1 matters once recording feeds off this subscription: with
        # KEEP_LAST depth=1, any executor scheduling hiccup overwrites the
        # not-yet-delivered frame and the recording loses it, even under
        # RELIABLE (reliability guarantees delivery into the history, not
        # that history won't be overwritten). depth=20 (~666ms-1s of slack)
        # costs at most ~20 * ~510KB per raw camera -- cheap defensive
        # headroom against executor scheduling stalls, raised from 5 while
        # investigating the insight9_a loss below (measured to make no
        # difference on its own -- see image_qos_reliability for the actual
        # fix -- but kept since it's free insurance against a slower one).
        image_qos = make_image_qos(depth=20, reliability=self.image_qos_reliability)
        for camera in self.cameras:
            namespace = camera.namespace
            align_topic = (
                image_topic(namespace, self._alignment_stream_for(camera))
                if self.live_alignment_available
                else None
            )
            also_alignment = self.live_alignment_available and align_topic == camera.topic
            msg_type = CompressedImage if camera.topic_type == "compressed" else RosImage
            sub = self.create_subscription(
                msg_type,
                camera.topic,
                self._make_dashboard_image_callback(camera.name, camera.topic_type, also_alignment=also_alignment),
                image_qos,
                callback_group=self.ros_callback_group,
                event_callbacks=self.subscription_event_callbacks,
            )
            self.dashboard_subscriptions.append(sub)
            # Playback shadow: PlaybackManager remaps `ros2 bag play` onto
            # /bagplay/... so replayed frames never share a topic with a
            # still-connected live camera (see _make_dashboard_image_callback).
            playback_sub = self.create_subscription(
                msg_type,
                bagplay_topic(camera.topic),
                self._make_dashboard_image_callback(
                    camera.name, camera.topic_type, also_alignment=also_alignment, is_live=False
                ),
                image_qos,
                callback_group=self.ros_callback_group,
                event_callbacks=self.subscription_event_callbacks,
            )
            self.dashboard_subscriptions.append(playback_sub)
            threading.Thread(
                target=self._frame_worker_loop,
                args=(camera.name, camera.topic_type, also_alignment),
                daemon=True,
                name=f"frame_worker_{camera.name}",
            ).start()
            self.get_logger().info(f"Images: {camera.name} <- {camera.topic} type={camera.topic_type}")

    def _create_hand_overlay_subscriptions(self) -> None:
        if Detection2DArray is None:
            return
        # Subscribed for every camera, not just insight9_a: hand detection is
        # entirely device-side (HandEngine), so whether a camera actually
        # publishes these topics is data-driven -- hand_overlay_available
        # only flips true once a message actually arrives (see hand_overlay.py).
        hand_qos = make_image_qos(depth=5, reliability="best_effort")
        for camera in self.cameras:
            namespace = camera.namespace
            hand_topic = f"/{namespace}/camera/hand"
            keypoints_topic = f"/{namespace}/camera/hand_keypoints"
            box_sub = self.create_subscription(
                Detection2DArray,
                hand_topic,
                lambda msg, name=camera.name: self._on_hand_boxes(name, msg),
                hand_qos,
                callback_group=self.ros_callback_group,
                event_callbacks=self.subscription_event_callbacks,
            )
            kp_sub = self.create_subscription(
                Detection2DArray,
                keypoints_topic,
                lambda msg, name=camera.name: self._on_hand_keypoints(name, msg),
                hand_qos,
                callback_group=self.ros_callback_group,
                event_callbacks=self.subscription_event_callbacks,
            )
            # Playback shadow: same live/bagplay dual-subscription split as
            # images/poses, so replayed hand landmarks drive the overlays
            # without ever blending with a still-connected live camera.
            box_playback_sub = self.create_subscription(
                Detection2DArray,
                bagplay_topic(hand_topic),
                lambda msg, name=camera.name: self._on_hand_boxes(name, msg, is_live=False),
                hand_qos,
                callback_group=self.ros_callback_group,
                event_callbacks=self.subscription_event_callbacks,
            )
            kp_playback_sub = self.create_subscription(
                Detection2DArray,
                bagplay_topic(keypoints_topic),
                lambda msg, name=camera.name: self._on_hand_keypoints(name, msg, is_live=False),
                hand_qos,
                callback_group=self.ros_callback_group,
                event_callbacks=self.subscription_event_callbacks,
            )
            self.dashboard_subscriptions.extend(
                [box_sub, kp_sub, box_playback_sub, kp_playback_sub]
            )

    def _log_perf_summary(self) -> None:
        snapshot = perf_tracker.snapshot_and_reset()
        if not snapshot["percent_of_one_core"]:
            return
        self.get_logger().info(perf_tracker.format_summary(snapshot))

    def _create_alignment_subscriptions(self) -> None:
        if not self.live_alignment_available:
            return
        image_qos = make_image_qos(reliability=self.image_qos_reliability)
        for camera in self.cameras:
            camera_name = camera.name
            namespace = camera.namespace
            alignment_stream = self._alignment_stream_for(camera)
            calib_topic = image_topic(namespace, alignment_stream)
            calib_info_topic = camera_info_topic(namespace, alignment_stream)
            calib_type = IMAGE_STREAMS[alignment_stream]["type"]
            self.live_alignment_topic_by_camera[camera_name] = calib_topic

            # Avoid duplicate subscription: if the alignment stream is the same topic
            # as the display stream, piggyback on the display callback rather than
            # creating a second RELIABLE subscriber to the same publisher.  Having two
            # simultaneous RELIABLE subscribers from the same node to the same topic
            # can trigger a DDS backpressure loop (depth=1 + slow Python GIL) that
            # permanently stalls one camera's entire participant.
            if calib_topic != camera.topic:
                calib_msg_type = CompressedImage if calib_type == "compressed" else RosImage
                calib_sub = self.create_subscription(
                    calib_msg_type,
                    calib_topic,
                    self._make_live_alignment_image_callback(camera_name, calib_type),
                    image_qos,
                    callback_group=self.ros_callback_group,
                    event_callbacks=self.subscription_event_callbacks,
                )
                self.dashboard_subscriptions.append(calib_sub)
            else:
                self.get_logger().info(
                    f"Alignment: {camera_name} shares display topic {calib_topic}; "
                    "alignment callback will be invoked from display subscription"
                )

            info_sub = self.create_subscription(
                CameraInfo,
                calib_info_topic,
                self._make_camera_info_callback(camera_name),
                make_qos(depth=2),
                callback_group=self.ros_callback_group,
                event_callbacks=self.subscription_event_callbacks,
            )
            self.dashboard_subscriptions.append(info_sub)
            self.get_logger().info(
                f"Alignment: {camera_name} image={calib_topic} info={calib_info_topic} type={calib_type}"
            )

    def _any_ros_data_received(self) -> bool:
        # GIL-atomic dict reads; values only ever go None->frame / 0.0->t
        # before the first user-triggered reset, so no locks needed for a
        # boolean "has anything ever arrived".
        return any(frame is not None for frame in self.latest_camera_frames.values()) or any(
            t > 0.0 for t in self.last_pose_received_time.values()
        )

    @staticmethod
    def _camera_link_up() -> bool:
        # A 169.254.x.x address on any non-loopback/docker interface is a
        # camera's point-to-point USB-ethernet link (see
        # scripts/reboot_cameras.sh) -- i.e. a camera is physically
        # connected, whether or not its ROS stack is publishing yet. Pure
        # ioctls (microseconds) rather than shelling out to `ip`, so the
        # watchdog poll is effectively free.
        try:
            names = os.listdir("/sys/class/net")
        except OSError:
            return False
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for name in names:
                if name == "lo" or name.startswith("docker"):
                    continue
                try:
                    packed = fcntl.ioctl(
                        sock.fileno(),
                        0x8915,  # SIOCGIFADDR
                        struct.pack("256s", name.encode()[:15]),
                    )
                except OSError:
                    continue  # interface has no IPv4 address
                if socket.inet_ntoa(packed[20:24]).startswith("169.254."):
                    return True
        finally:
            sock.close()
        return False

    def _restart_for_stale_participant(self, reason: str) -> None:
        # Shared exit path for both watchdog cases below. Fast DDS enumerates
        # network interfaces only at participant creation, so a link that
        # appeared/changed after this process started stays invisible to it
        # until the participant is recreated, i.e. until this process
        # restarts. Inside docker, `restart: unless-stopped` (docker-compose.yml)
        # brings it back with the current links present; outside docker there
        # is no such policy, so warn instead of exiting into nothing.
        if os.path.exists("/.dockerenv"):
            self.get_logger().error(
                f"{reason} -- exiting so the container restart policy recreates the DDS participant."
            )
            os._exit(1)
        self.get_logger().warning(f"{reason} -- restart this process to recover.")

    def _recording_active(self) -> bool:
        manager = self.recording_manager
        if manager is None:
            return False
        try:
            return manager.is_recording()
        except Exception:
            return False

    def _stale_participant_watchdog_loop(self) -> None:
        # Fast DDS enumerates network interfaces only at participant
        # creation, so two related failure modes share the same fix
        # (recreate the participant, i.e. restart this process):
        #
        # 1. Boot race: this container auto-starts at host boot (restart:
        #    unless-stopped), usually before the per-camera USB-ethernet
        #    links exist, so the participant advertises unicast locators the
        #    cameras can't route to and never receives a single message.
        #    That state does NOT self-heal (observed fully stale >15min
        #    while a fresh `ros2 topic list` in the same container saw
        #    every topic instantly). run_dashboard.sh has the same
        #    link-presence check, but only runs once when someone invokes
        #    the script -- this covers headless boots too.
        # 2. Runtime drop: a camera that WAS streaming loses its link (USB
        #    unplugged and replugged) and never comes back on its own, for
        #    the same interface-enumeration reason. Case 1's "any data ever
        #    received" check can't see this -- the other cameras are still
        #    flowing -- so this needs its own per-camera staleness check.
        #
        # This thread never retires: case 1 can only ever fire once per
        # process (after that, "some data has arrived" is permanent), but
        # case 2 needs to keep watching for the life of the process.
        link_grace_sec = 60.0
        poll_sec = 5.0
        # Generous vs. camera_stale_timeout_sec (the UI's "no signal" flag,
        # default 2s) so a brief frame gap or exposure hiccup can't trigger a
        # restart -- this only fires on a camera that stays silent.
        camera_stall_grace_sec = 15.0
        link_up_since: Optional[float] = None
        while True:
            time.sleep(poll_sec)
            now = time.monotonic()

            if not self._any_ros_data_received():
                if not self._camera_link_up():
                    link_up_since = None
                    continue
                if link_up_since is None:
                    link_up_since = now
                    continue
                if now - link_up_since < link_grace_sec:
                    continue
                self._restart_for_stale_participant(
                    "Camera link up for 60s but no ROS data ever received -- DDS participant "
                    "likely predates the camera links"
                )
                link_up_since = now
                continue

            link_up_since = None

            if self._recording_active():
                # Don't kill an in-progress recording over one stalled
                # camera -- the others are still capturing fine, and a
                # full-process restart (the only fix DDS gives us) would cut
                # all of them short over one dropout.
                continue

            if not self._camera_link_up():
                # No camera USB-ethernet link exists, so "frames stopped" is
                # not a recoverable link drop and a restart fixes nothing.
                # Concretely: on a machine with no cameras attached, bag
                # playback populates camera_frame_times, and when the bag
                # ends the stall check below would restart-loop the backend
                # (observed 2026-07-12 on the dev machine after the fleet
                # moved to another device).
                continue

            for camera in self.cameras:
                frame_times = self.camera_frame_times[camera.name]
                if not frame_times or now - frame_times[-1] <= camera_stall_grace_sec:
                    continue
                self._restart_for_stale_participant(
                    f"Camera '{camera.name}' produced no frames for over "
                    f"{camera_stall_grace_sec:.0f}s after previously streaming "
                    "(likely a USB/link drop)"
                )
                break

    def _make_pose_callback(self, pose_name: str, is_live: bool = True):
        def callback(msg: PoseStamped) -> None:
            if is_live == self._playback_mode:
                return
            stamp_ns = self._stamp_to_ns(msg.header.stamp)
            pose_sample = PoseSample(
                stamp_ns=stamp_ns,
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
            # Live-only: calibration doesn't change bag-to-bag, and this
            # feeds live board alignment, not the playback display -- no
            # shadow /bagplay subscription needed, just stay inert while
            # viewing a replay.
            if self._playback_mode:
                return
            self.live_alignment_camera_matrix[camera_name] = np.array(msg.k, dtype=np.float64).reshape((3, 3))
            self.live_alignment_dist_coeffs[camera_name] = np.array(msg.d, dtype=np.float64).reshape((-1, 1))

        return callback

    def start_image_recording(self, topic_output_paths: Dict[str, str]) -> None:
        """topic_output_paths maps each image topic to its own bag output
        path -- one InProcessBagWriter (and background thread) per distinct
        path, so e.g. insight3_a's and insight3_b's large uncompressed
        streams don't serialize behind each other on a single writer thread
        (measured: sharing one writer capped both around 13-16Hz instead of
        their ~20Hz native rate, while insight9_a's much smaller compressed
        stream alone kept up fine)."""
        with self._recording_writer_lock:
            if self._recording_writers:
                raise RuntimeError("Image recording writer is already running.")
            writers_by_path: Dict[str, InProcessBagWriter] = {}
            writer_by_topic: Dict[str, InProcessBagWriter] = {}
            for topic, output_path in topic_output_paths.items():
                writer = writers_by_path.get(output_path)
                if writer is None:
                    # The compressed Insight9 stream averages ~133 KiB at
                    # 30 Hz. Its two 128-entry writer stages only absorb
                    # about 8.5s total; a measured transient SQLite/writeback
                    # stall exceeded that and rejected 109 otherwise
                    # continuous frames. A later shared writeback stall also
                    # overflowed both 20 Hz raw writers at depth 128 (86
                    # frames each) while their received header sequences
                    # remained continuous. Use 512 for every image writer:
                    # about 34s of two-stage headroom for the compressed
                    # stream and 51s for each raw stream. The two raw streams'
                    # worst-case combined backlog is about 1 GiB, within this
                    # machine's measured memory headroom.
                    writer = InProcessBagWriter(
                        output_path,
                        max_queue=512,
                        storage_config_uri=str(STORAGE_CONFIG_PATH) if STORAGE_CONFIG_PATH.is_file() else "",
                    )
                    writers_by_path[output_path] = writer
                writer_by_topic[topic] = writer
            self._recording_writers = writers_by_path
            self._recording_writer_by_topic = writer_by_topic
            self._recording_timestamp_offsets_ns = {topic: None for topic in topic_output_paths}
            self._recording_header_audit = {
                topic: {"count": 0, "first_ns": None, "last_ns": None, "missing": 0,
                        "gap_events": 0, "worst_gap_ns": 0}
                for topic in topic_output_paths
            }

    def stop_image_recording(self) -> Dict[str, object]:
        with self._recording_writer_lock:
            writers = self._recording_writers
            self._recording_writers = {}
            self._recording_writer_by_topic = {}
            self._recording_timestamp_offsets_ns = {}
            audit = self._finalize_image_header_audit()
            self._recording_header_audit = {}
        dropped = 0
        dropped_by_topic: Dict[str, int] = {}
        for writer in writers.values():
            writer.close()
            dropped += writer.dropped_count
            for topic, count in writer.dropped_by_topic.items():
                dropped_by_topic[topic] = dropped_by_topic.get(topic, 0) + int(count)
        # A continuous received-header sequence is not sufficient if the
        # bounded disk queue rejected a frame after this callback observed it.
        audit["writer_queue_dropped"] = dropped
        audit["writer_queue_dropped_by_topic"] = dropped_by_topic
        for topic, topic_audit in audit.get("topics", {}).items():
            topic_dropped = int(dropped_by_topic.get(topic, 0))
            topic_audit["writer_queue_dropped"] = topic_dropped
            topic_audit["ok"] = bool(topic_audit.get("ok")) and topic_dropped == 0
        audit["ok"] = bool(audit["ok"]) and dropped == 0
        return {"dropped": dropped, "image_header_audit": audit}

    def _finalize_image_header_audit(self) -> Dict[str, object]:
        """Return the in-flight image continuity report without re-reading a bag."""
        topics: Dict[str, object] = {}
        for topic, stat in self._recording_header_audit.items():
            count = int(stat["count"])
            first_ns = stat["first_ns"]
            last_ns = stat["last_ns"]
            nominal_hz = nominal_for(topic)
            span_s = ((int(last_ns) - int(first_ns)) / 1e9) if count > 1 and first_ns is not None and last_ns is not None else 0.0
            topics[topic] = {
                "frames": count,
                "nominal_hz": nominal_hz,
                "observed_hz": round((count - 1) / span_s, 3) if span_s > 0 else None,
                "missing": int(stat["missing"]),
                "gap_events": int(stat["gap_events"]),
                "worst_gap_ms": round(int(stat["worst_gap_ns"]) / 1e6, 3),
                "ok": count > 1 and int(stat["missing"]) == 0,
            }
        return {"method": "live_image_header_audit", "topics": topics,
                "ok": bool(topics) and all(item["ok"] for item in topics.values())}

    def _feed_recording_writer(self, topic: str, msg: object) -> None:
        writer = self._recording_writer_by_topic.get(topic)
        if writer is None:
            return
        now_ns = self.get_clock().now().nanoseconds
        header = getattr(msg, "header", None)
        stamp = getattr(header, "stamp", None)
        source_ns = int(getattr(stamp, "sec", 0)) * 1_000_000_000 + int(getattr(stamp, "nanosec", 0))
        if source_ns <= 0:
            # This is not expected for our image messages, but retaining a
            # usable fallback keeps recording compatible with a malformed or
            # headerless custom image message.
            writer.write(topic, msg, now_ns)
            return

        audit = self._recording_header_audit.get(topic)
        if audit is not None:
            previous_ns = audit["last_ns"]
            if previous_ns is not None:
                gap_ns = source_ns - int(previous_ns)
                nominal_hz = nominal_for(topic)
                if nominal_hz and gap_ns > 1.5e9 / nominal_hz:
                    audit["gap_events"] = int(audit["gap_events"]) + 1
                    audit["missing"] = int(audit["missing"]) + max(0, round(gap_ns * nominal_hz / 1e9) - 1)
                    audit["worst_gap_ns"] = max(int(audit["worst_gap_ns"]), gap_ns)
            if audit["first_ns"] is None:
                audit["first_ns"] = source_ns
            audit["last_ns"] = source_ns
            audit["count"] = int(audit["count"]) + 1

        offset_ns = self._recording_timestamp_offsets_ns.get(topic)
        if offset_ns is None:
            # The source clocks are boot-relative.  Anchor each stream once,
            # then write subsequent samples using source time so a 30-80 ms
            # Python/DDS scheduling pause cannot look like a dropped frame in
            # the resulting bag (the complete frames are often delivered in
            # a short burst immediately afterwards).
            offset_ns = now_ns - source_ns
            self._recording_timestamp_offsets_ns[topic] = offset_ns
        writer.write(topic, msg, source_ns + offset_ns)

    def _make_dashboard_image_callback(
        self, camera_name: str, topic_type: str, also_alignment: bool = False, is_live: bool = True
    ):
        camera_topic = next(c.topic for c in self.cameras if c.name == camera_name)
        event = self._pending_frame_events[camera_name]

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
        # one side is ever authoritative: is_live == self._playback_mode means
        # "wrong source for the current mode" -- a live camera still
        # connected during playback, or a stray playback message lingering
        # after playback stopped -- so it's dropped before display, never
        # blended by timestamp guessing (camera header stamps are boot-
        # relative, not epoch, so they can't disambiguate the two).
        def callback(msg) -> None:
            if is_live == self._playback_mode:
                return
            if not self._playback_mode:
                self._feed_recording_writer(camera_topic, msg)
            self._pending_frames[camera_name] = msg
            event.set()

        return callback

    def _frame_worker_loop(self, camera_name: str, topic_type: str, also_alignment: bool) -> None:
        alignment_cb = self._make_live_alignment_image_callback(camera_name, topic_type) if also_alignment else None
        event = self._pending_frame_events[camera_name]
        while rclpy is not None and rclpy.ok():
            if not event.wait(timeout=1.0):
                continue
            event.clear()
            msg = self._pending_frames.pop(camera_name, None)
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
                if self._recording_active() and self._webrtc_has_sessions.get(camera_name):
                    min_interval = 1.0 / RECORDING_WEBRTC_PREVIEW_FPS
                    previous = self._last_recording_preview_at.get(camera_name, 0.0)
                    if preview_now - previous < min_interval:
                        continue
                    self._last_recording_preview_at[camera_name] = preview_now
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
                    camera_name in self.gripper_tracking_cameras
                    or self._hw_jpeg is None
                    or not self._hw_jpeg.can_encode_ros_image(camera_name, msg)
                ):
                    display_image = self._decode_display_image(msg)
                if camera_name in self.gripper_tracking_cameras:
                    gripper_image = (
                        display_image
                        if display_image is not None
                        else self._decode_calibration_message(topic_type, msg)
                    )
                    if gripper_image is not None:
                        self._process_gripper_image(camera_name, gripper_image)
                now = time.monotonic()
                # Compressed input is already a JPEG, but raw input would
                # otherwise pay an NVJPEG encode for every frame solely for
                # an HTTP fallback hidden behind an active WebRTC <video>.
                # Refresh that fallback twice a second instead.
                refresh_fallback = (
                    topic_type == "compressed"
                    or not self._webrtc_has_sessions.get(camera_name)
                    or now - self._last_webrtc_fallback_jpeg_at.get(camera_name, 0.0)
                    >= WEBRTC_JPEG_FALLBACK_INTERVAL_SEC
                )
                frame = (
                    self._encode_dashboard_frame(camera_name, topic_type, msg, display_image)
                    if refresh_fallback
                    else None
                )
                with self.camera_frame_lock:
                    # Once an overlay has been dispatched, keep the last
                    # composited frame onscreen until its replacement returns
                    # from the worker. Replacing it with every raw source
                    # frame made the overlay visible only for the fraction of
                    # a frame between the asynchronous result and the next
                    # camera callback.
                    if frame is not None and not frame.hand_overlay_pending:
                        self.latest_camera_frames[camera_name] = frame
                        if topic_type != "compressed":
                            self._last_webrtc_fallback_jpeg_at[camera_name] = frame.received_monotonic
                    self.camera_frame_times[camera_name].append(now)
                self._maybe_queue_webrtc_frame(camera_name, topic_type, msg, frame)
            except Exception as exc:  # noqa: BLE001 - a bad frame must not kill the worker
                self.get_logger().warning(f"frame worker {camera_name}: {exc}")

    def _maybe_queue_webrtc_frame(self, camera_name: str, topic_type: str, msg, frame) -> None:
        """Hand a frame to the webrtc_worker process's send queue, unless
        nobody's watching that camera over WebRTC right now.

        This check has to happen here, before resolving/copying any bytes --
        moving it to the IPC send thread instead would mean every frame
        still pays for the resolution work below even with zero viewers,
        exactly the per-frame cost push_ros_frame() used to skip in-process
        (see webrtc_stream.py's push_resolved_frame docstring). Gating state
        comes from webrtc_worker.py's create_session/close_session
        transitions, relayed over IPC into self._webrtc_has_sessions (see
        _webrtc_ipc_loop).
        """
        if not self._webrtc_has_sessions.get(camera_name):
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
        self._pending_webrtc_frames[camera_name] = (fmt, width, height, data)
        self._webrtc_frame_event.set()

    def _start_webrtc_worker(self) -> "subprocess.Popen":
        script_path = Path(__file__).resolve().parent / "webrtc_worker.py"
        env = dict(os.environ)
        env["INSIGHT_WEBRTC_AUTHKEY"] = self._webrtc_authkey.hex()
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
        log_path = self.project_root / "outputs" / "webrtc_worker.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "a", buffering=1)
        proc = subprocess.Popen(
            [
                sys.executable,
                str(script_path),
                "--config",
                str(self.config_path),
                "--webrtc-port",
                str(self.webrtc_port),
                "--ipc-socket",
                self._webrtc_ipc_path,
            ],
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        self.get_logger().info(f"webrtc: spawned webrtc_worker.py pid={proc.pid} port={self.webrtc_port}")
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
        script_path = Path(__file__).resolve().parent / "hand_overlay_worker.py"
        env = dict(os.environ)
        env["INSIGHT_HANDOVERLAY_AUTHKEY"] = self._hand_overlay_authkey.hex()
        log_path = self.project_root / "outputs" / "hand_overlay_worker.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "a", buffering=1)
        proc = subprocess.Popen(
            [
                sys.executable,
                str(script_path),
                "--ipc-socket",
                self._hand_overlay_ipc_path,
            ],
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        self.get_logger().info(f"hand_overlay: spawned hand_overlay_worker.py pid={proc.pid}")
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
        self._pending_hand_overlay_frames[camera_name] = (version, jpeg_bytes, hands)
        self._hand_overlay_frame_event.set()

    def _hand_overlay_ipc_loop(self) -> None:
        """Owns the single connection to hand_overlay_worker.py: sends
        dispatched frames and applies composited results back into
        latest_camera_frames. Single thread on this side too (mirrors
        IpcServer in hand_overlay_worker.py) so no lock is needed around
        the Connection itself."""
        authkey = self._hand_overlay_authkey
        while rclpy is not None and rclpy.ok():
            try:
                conn = Client(self._hand_overlay_ipc_path, family="AF_UNIX", authkey=authkey)
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
                        self._apply_composited_hand_overlay(camera_name, version, composited)
                    if self._hand_overlay_frame_event.wait(timeout=0.05):
                        self._hand_overlay_frame_event.clear()
                        for camera_name in list(self._pending_hand_overlay_frames.keys()):
                            payload = self._pending_hand_overlay_frames.pop(camera_name, None)
                            if payload is None:
                                continue
                            conn.send((camera_name,) + payload)
            except (EOFError, OSError) as exc:
                self.get_logger().warning(f"hand overlay ipc: lost connection to worker ({exc}); reconnecting")
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
        if version <= self._hand_overlay_last_applied.get(camera_name, -1):
            return
        width, height = self._jpeg_dimensions(composited)
        with self.camera_frame_lock:
            current = self.latest_camera_frames.get(camera_name)
            if current is None:
                return
            self._hand_overlay_last_applied[camera_name] = version
            self.latest_camera_frames[camera_name] = CameraFrame(
                data=composited,
                stamp_ns=current.stamp_ns,
                received_monotonic=time.monotonic(),
                mime_type="image/jpeg",
                width=width,
                height=height,
                version=version,
            )
        if self._webrtc_has_sessions.get(camera_name):
            self._pending_webrtc_frames[camera_name] = ("JPEG", width, height, composited)
            self._webrtc_frame_event.set()

    def _webrtc_ipc_loop(self) -> None:
        """Owns the single connection to webrtc_worker.py: sends frames the
        worker actually has viewers for, and relays its session_state
        updates back into self._webrtc_has_sessions. Single thread on this
        side too (mirrors IpcServer in webrtc_worker.py) so no lock is
        needed around the Connection itself."""
        authkey = self._webrtc_authkey
        while rclpy is not None and rclpy.ok():
            try:
                conn = Client(self._webrtc_ipc_path, family="AF_UNIX", authkey=authkey)
            except OSError:
                time.sleep(1.0)
                continue
            try:
                while rclpy is not None and rclpy.ok():
                    while conn.poll(0):
                        message = conn.recv()
                        if isinstance(message, tuple) and len(message) == 3 and message[0] == "session_state":
                            _, camera_name, has_sessions = message
                            self._webrtc_has_sessions[camera_name] = bool(has_sessions)
                    if self._webrtc_frame_event.wait(timeout=0.05):
                        self._webrtc_frame_event.clear()
                        for camera_name in list(self._pending_webrtc_frames.keys()):
                            payload = self._pending_webrtc_frames.pop(camera_name, None)
                            if payload is None:
                                continue
                            conn.send((camera_name,) + payload)
            except (EOFError, OSError) as exc:
                self.get_logger().warning(f"webrtc ipc: lost connection to worker ({exc}); reconnecting")
            finally:
                with contextlib.suppress(Exception):
                    conn.close()
            time.sleep(1.0)

    def _webrtc_healthz_loop(self) -> None:
        """Polls webrtc_worker.py's own /healthz every 5s -- this is the
        replacement for the old in-process `self.webrtc_streams is not
        None` check in build_camera_payload. Deliberately dumb (blocking
        stdlib http.client, short timeout, wide try/except): the worker not
        being up yet/having crashed/lacking hardware elements must all just
        read as unavailable here, same as today's fallback-to-polling
        behavior, not raise."""
        while rclpy is not None and rclpy.ok():
            available = False
            try:
                conn = http.client.HTTPConnection("127.0.0.1", self.webrtc_port, timeout=2.0)
                try:
                    conn.request("GET", "/healthz")
                    resp = conn.getresponse()
                    payload = json.loads(resp.read())
                    available = bool(payload.get("webrtc_available"))
                finally:
                    conn.close()
            except Exception:
                available = False
            self._webrtc_available_cached = available
            time.sleep(5.0)

    def _encode_dashboard_frame(
        self,
        camera_name: str,
        topic_type: str,
        msg: object,
        decoded_image: Optional[np.ndarray] = None,
    ) -> Optional[CameraFrame]:
        stamp_ns = self._stamp_to_ns(msg.header.stamp)
        received_monotonic = time.monotonic()
        with self.camera_frame_lock:
            version = self.camera_frame_versions.get(camera_name, 0) + 1
            self.camera_frame_versions[camera_name] = version
        if topic_type == "compressed":
            data = bytes(msg.data)
            hand_overlay_pending = False
            if camera_name in self.hand_overlay_enabled:
                # Gates the frame and, if worth overlaying, dispatches the
                # actual decode/draw/re-encode to hand_overlay_worker.py --
                # see compose_hand_overlay_jpeg's docstring for why that work
                # can't happen inline here anymore. This tick still serves
                # the plain passthrough `data`; the composited version lands
                # asynchronously and patches into latest_camera_frames once
                # ready (_hand_overlay_ipc_loop), so a served frame is
                # undecorated for at most one IPC round trip.
                hand_overlay_pending = self.compose_hand_overlay_jpeg(camera_name, data, version)
            width, height = self._jpeg_dimensions(data)
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
        if self._hw_jpeg is not None:
            with track(f"image_encode_hw:{camera_name}"):
                hw_result = self._hw_jpeg.encode_ros_image(camera_name, msg, quality=82)
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
        image = decoded_image if decoded_image is not None else self._decode_display_image(msg)
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
        return self._decode_calibration_message("image", msg)

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
            # Live-only, same reasoning as _make_camera_info_callback.
            if self._playback_mode or not self.live_alignment_active:
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
            self.raw_traces[pose_name].append(pose_sample.position)

    def clear_traces(self) -> None:
        with self.pose_lock:
            for name in self.raw_traces:
                self.raw_traces[name].clear()
            for name in self.last_pose_received_time:
                self.last_pose_received_time[name] = 0.0

    def set_playback_mode(self, enabled: bool) -> None:
        # Live vs. playback is now decided structurally by which topic a
        # message arrived on (see bagplay_topic / _make_dashboard_image_callback),
        # not by comparing header timestamps to the bag's time range -- Insight
        # camera stamps are boot-relative and can't carry that comparison.
        self._playback_mode = enabled
        self.get_logger().info(f"Playback mode {'ON' if enabled else 'OFF'}")

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
        hand_entries = []  # payload dicts for visible hands
        with self.pose_lock:
            for pose in self.poses:
                transformed = self.transformed_pose_sample(pose.name)
                raw_sample = self.latest_pose_sample.get(pose.name)
                visible = raw_sample is not None and (self.fake_pose or (now - self.last_pose_received_time[pose.name]) <= self.pose_timeout_sec)
                if transformed is None:
                    position = [0.0, 0.0, 0.0]
                    quaternion = [0.0, 0.0, 0.0, 1.0]
                else:
                    # Rounded to 0.01mm (position) / 1e-5 (quaternion, unitless) --
                    # this stream broadcasts at ~20Hz with a full trace history
                    # (up to max_points) resent every tick, so untruncated
                    # float64 repr (~17 sig figs) was bloating each message to
                    # ~60KB and both server-side json.dumps and client-side
                    # parse/render of that at 20Hz was the actual source of
                    # the trajectory lag -- far beyond what this visualization
                    # needs precision-wise.
                    position = [round(float(value), 5) for value in transformed.position]
                    quaternion = [round(float(value), 5) for value in transformed.orientation_xyzw]
                trace_points = self.transformed_trace(pose.name)
                # np.round over the whole trace in C instead of a 300-iteration
                # Python loop -- measured ~2x faster, and less GIL hold time
                # per broadcast tick.
                trace = np.round(np.asarray(trace_points, dtype=np.float64), 4).tolist() if trace_points else []
                entry = {
                    "name": pose.name,
                    "role": pose.teleop_role,
                    "visible": visible,
                    "position": position,
                    "quaternion_xyzw": quaternion,
                    "trace": trace,
                    "avatar_model": pose.avatar_model,
                    "avatar_scale": pose.avatar_scale,
                    "avatar_rotation_deg_xyz": [float(value) for value in pose.avatar_rotation_deg_xyz],
                    "avatar_offset_xyz": [float(value) for value in pose.avatar_offset_xyz],
                    "gripper_opening": self.gripper_opening_percent(pose.name),
                }
                poses.append(entry)
                if transformed is not None and visible and pose.teleop_role in ("left_hand", "right_hand"):
                    hand_entries.append(entry)
        # Stick-figure extra: the latest normalized 21-point hand shape (see
        # hand_landmarks_for_role; None until a HandEngine camera has
        # detected that hand), same dashboard frame as `position`.
        for entry in hand_entries:
            entry["hand_landmarks"] = self.hand_landmarks_for_role(entry["role"])
        return {
            "type": "pose_update",
            "timestamp_ms": int(time.time() * 1000),
            "fake_pose": self.fake_pose,
            "playback_mode": self._playback_mode,
            "stick_figure_mode": bool(self.stick_figure_mode),
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
            "camera_names": [camera.name for camera in self.cameras],
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
                stale = frame is None or (now - frame.received_monotonic) > self.camera_stale_timeout_sec
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
                        "webrtc_available": self._webrtc_available_cached,
                        "webrtc_port": self.webrtc_port,
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

    def build_settings_payload(self) -> Dict[str, object]:
        hand_cameras = set(getattr(self, "gripper_calibrations", {}).keys())
        poses = []
        for pose in self.poses:
            model_name = Path(pose.avatar_model).name if pose.avatar_model else None
            entry = {
                "name": pose.name,
                "role": pose.teleop_role,
                "avatar_model": model_name,
            }
            if pose.name in hand_cameras:
                entry["gripper_tracking_available"] = True
                entry["gripper_tracking_enabled"] = pose.name in self.gripper_tracking_cameras
            if pose.name in getattr(self, "hand_overlay_available", set()):
                entry["hand_overlay_available"] = True
                entry["hand_overlay_enabled"] = pose.name in self.hand_overlay_enabled
            poses.append(entry)
        return {
            "poses": poses,
            "available_models": AVAILABLE_AVATAR_MODELS,
            "stick_figure_mode": bool(self.stick_figure_mode),
        }

    def set_pose_avatar_model(self, pose_name: str, model_file: str) -> PoseSpec:
        pose = next((p for p in self.poses if p.name == pose_name), None)
        if pose is None:
            raise ValueError(f"Unknown camera/pose '{pose_name}'")
        allowed = {entry["file"] for entry in AVAILABLE_AVATAR_MODELS}
        if model_file not in allowed:
            raise ValueError(f"'{model_file}' is not one of the available models")
        defaults = avatar_model_defaults(model_file)
        # In-memory only, like the rest of Settings -- resets to cameras.json's
        # configured value on the next process restart rather than persisting,
        # since nothing else here writes back to the JSON config files.
        pose.avatar_model = f"assets/models/{model_file}"
        pose.avatar_scale = float(defaults.get("avatar_scale", 1.0))
        pose.avatar_rotation_deg_xyz = tuple(defaults.get("avatar_rotation_deg_xyz", [0.0, 0.0, 0.0]))
        pose.avatar_offset_xyz = (0.0, 0.0, 0.0)
        return pose

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

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path(__file__).resolve().parents[1] / "config" / "cameras.json"))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--webrtc-port", type=int, default=8766)
    parser.add_argument("--web-root", default=str(Path(__file__).resolve().parents[1] / "web_dashboard" / "dist"))
    parser.add_argument("--view-mode", choices=("3d",), default="3d")
    parser.add_argument("--fake-pose", action="store_true")
    # Fallback only -- overridden by dashboard.trajectory.pose_publish_hz in
    # cameras.json when present (see PoseBridgeNode.__init__). Raised to 50Hz
    # 2026-07-22; be aware this broadcast worker measurably competes with
    # concurrent WebRTC sessions for the GIL with idle CPU sitting unused
    # (see wiki changelog) -- the webrtc_stream.py process split is meant to
    # remove that specific interaction, re-verify fps under load if either
    # side of that split changes.
    parser.add_argument("--pose-publish-hz", type=float, default=50.0)
    parser.add_argument("--start-alignment", action="store_true")
    parser.add_argument("--post-processing-config", default=str(Path(__file__).resolve().parents[1] / "config" / "post_processing.json"))
    parser.add_argument("--rosbag-dir", "-rosbag-dir", default=None)
    return parser.parse_args()


def _run_executor(executor: "MultiThreadedExecutor", node: "PoseBridgeNode") -> None:
    # executor.spin() runs in a daemon thread: if it raises (observed in the
    # wild as rclpy's RCLError "failed to initialize wait set" on a startup
    # race), the exception is otherwise silently swallowed -- the process
    # keeps running, the HTTP server keeps answering healthz, but every ROS
    # callback (pose/image/camera_info) is dead forever with no visible
    # signal that anything is wrong. Exit hard so `restart: unless-stopped`
    # actually gets a chance to recover instead of leaving a zombie backend.
    try:
        executor.spin()
    except Exception:
        node.get_logger().fatal(
            "ROS executor thread crashed; exiting so the container restarts.\n"
            + traceback.format_exc()
        )
        os._exit(1)


def main() -> None:
    # Crash forensics: docker's stdout capture loses the final unflushed
    # buffer on os._exit/segfault, so post-mortems from `docker logs` are
    # unreliable. Dump native crashes and every thread's stack on
    # SIGINT/SIGTERM to a host-mounted file instead (chain=True keeps
    # rclpy's own signal handling intact).
    import faulthandler
    import signal

    crash_log_path = Path(__file__).resolve().parents[1] / "outputs" / "backend_crash.log"
    crash_log_path.parent.mkdir(parents=True, exist_ok=True)
    crash_log = open(crash_log_path, "a", buffering=1)
    crash_log.write(f"--- backend start pid={os.getpid()} time={time.time():.0f} ---\n")
    faulthandler.enable(file=crash_log, all_threads=True)
    for sig in (signal.SIGTERM, signal.SIGINT):
        faulthandler.register(sig, file=crash_log, all_threads=True, chain=True)
    # On-demand live thread dump (chain=False: SIGUSR1 has no default action
    # once handled, so this never terminates the process) -- kill -USR1 <pid>
    # to see every thread's current stack without killing the backend.
    faulthandler.register(signal.SIGUSR1, file=crash_log, all_threads=True, chain=False)

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

    rclpy.init(args=None)
    enable_alignment_stream = not args.fake_pose
    node = PoseBridgeNode(
        config_path,
        fake_pose=args.fake_pose,
        pose_publish_hz=args.pose_publish_hz,
        enable_alignment_stream=enable_alignment_stream,
        webrtc_port=args.webrtc_port,
    )
    node.get_logger().info(f"View mode={args.view_mode} alignment_stream={enable_alignment_stream}")

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
        image_topics=[camera.topic for camera in node.cameras],
        start_image_recording=node.start_image_recording,
        stop_image_recording=node.stop_image_recording,
    )
    # Adopt recordings orphaned in rosbags/_staging/ by a power cut or crash
    # (reindex/salvage + merge into a normal bag, in the background).
    recording_manager.start_orphan_recovery()
    node.recording_manager = recording_manager
    if args.start_alignment and node.live_alignment_available and not args.fake_pose:
        node.start_live_alignment()
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    spin_thread = threading.Thread(
        target=_run_executor, args=(executor, node), daemon=True, name="ros_executor"
    )
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
        node.stop_webrtc_worker()
        node.stop_hand_overlay_worker()
        node.destroy_node()
        with contextlib.suppress(Exception):
            rclpy.shutdown()
        spin_thread.join(timeout=1.0)


if __name__ == "__main__":
    main()
