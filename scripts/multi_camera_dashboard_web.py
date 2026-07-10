#!/usr/bin/env python3

import argparse
import asyncio
import contextlib
import fcntl
import json
import math
import os
import shutil
import socket
import struct
import subprocess
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass
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
from check_bag import analyze_bag
from gripper_tracking import GripperTrackingMixin
from hand_overlay import HandOverlayMixin
from hw_jpeg import HwJpegCodec
from inprocess_bag_writer import InProcessBagWriter
from webrtc_stream import WebRtcStreams
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


def _read_tum_points(path: Path, max_points: int = 2000) -> list:
    """Read a TUM trajectory file and return a downsampled list of [x, y, z] points."""
    if not path.exists():
        return []
    points = []
    try:
        with path.open("r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) >= 4:
                    points.append([float(parts[1]), float(parts[2]), float(parts[3])])
    except Exception:
        return []
    if len(points) <= max_points:
        return points
    step = len(points) / max_points
    return [points[int(i * step)] for i in range(max_points)]


def read_system_load() -> Dict[str, object]:
    """Host-wide load average (not container-scoped -- /proc/loadavg isn't
    namespaced) plus this container's own cgroup v2 CPU quota, so the UI can
    show "how close to the ceiling are we" rather than a raw core count that
    doesn't mean much once the container is itself capped below the host's
    physical cores (see docker-compose.yml's `cpus:` limit).
    """
    load_1min = load_5min = None
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        load_1min, load_5min = float(parts[0]), float(parts[1])
    except (OSError, ValueError, IndexError):
        pass

    cpu_quota_cores = None
    try:
        with open("/sys/fs/cgroup/cpu.max") as f:
            quota_str, period_str = f.read().split()
        if quota_str != "max":
            cpu_quota_cores = int(quota_str) / int(period_str)
    except (OSError, ValueError):
        pass

    cpu_count = os.cpu_count() or 1
    budget = cpu_quota_cores or cpu_count
    return {
        "load_1min": load_1min,
        "load_5min": load_5min,
        "cpu_count": cpu_count,
        "cpu_quota_cores": cpu_quota_cores,
        "load_ratio": (load_1min / budget) if load_1min is not None else None,
    }


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


class PoseBridgeNode(LiveAlignmentMixin, GripperTrackingMixin, HandOverlayMixin, Node):
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

        raw_config = load_setup(config_path)
        config = build_dashboard_config(raw_config)
        enabled_camera_map = {
            camera["name"]: camera for camera in raw_config.get("cameras", []) if camera.get("enabled", True)
        }
        self.project_root = config_path.resolve().parents[1]
        self.window_title = config.get("window_title", "Insight Web Dashboard")
        trajectory_config = config.get("trajectory", {})
        self.image_qos_reliability = str(trajectory_config.get("image_qos_reliability", "best_effort"))
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

        self._playback_mode: bool = False
        self._bag_time_range: Optional[Tuple[int, int]] = None
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
        self.dashboard_subscriptions = []
        # Recording feeds off this node's own image subscriptions instead of
        # a second `ros2 bag record` reader -- see inprocess_bag_writer.py
        # for why a second reader on the same image topic causes drops.
        # Keyed by output path (one writer/thread per camera, see
        # start_image_recording) so cameras don't serialize behind each
        # other on a single writer thread.
        self._recording_writers: Dict[str, InProcessBagWriter] = {}
        self._recording_writer_by_topic: Dict[str, InProcessBagWriter] = {}
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
        # Hardware H.264 WebRTC streams for the camera panels; None when the
        # GStreamer WebRTC stack is unavailable, and the frontend keeps its
        # polling path as fallback either way (see webrtc_stream.py).
        self.webrtc_streams = WebRtcStreams.create(log=self.get_logger().info)
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
                self._make_pose_callback(pose.name),
                pose_qos,
                callback_group=self.ros_callback_group,
            )
            self.dashboard_subscriptions.append(sub)
            self.get_logger().info(f"Trajectory: {pose.name} <- {pose.topic}")

    def _alignment_stream_for(self, camera: "CameraSpec") -> str:
        return camera.alignment_image_stream or self.live_alignment_image_stream

    def _create_dashboard_image_subscriptions(self) -> None:
        # depth>1 matters once recording feeds off this subscription: with
        # KEEP_LAST depth=1, any executor scheduling hiccup overwrites the
        # not-yet-delivered frame and the recording loses it, even under
        # RELIABLE (reliability guarantees delivery into the history, not
        # that history won't be overwritten). 5 * ~510KB per camera is cheap.
        image_qos = make_image_qos(depth=5, reliability=self.image_qos_reliability)
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
            )
            self.dashboard_subscriptions.append(sub)
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
            )
            kp_sub = self.create_subscription(
                Detection2DArray,
                keypoints_topic,
                lambda msg, name=camera.name: self._on_hand_keypoints(name, msg),
                hand_qos,
                callback_group=self.ros_callback_group,
            )
            self.dashboard_subscriptions.extend([box_sub, kp_sub])

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

    def _stale_participant_watchdog_loop(self) -> None:
        # Fast DDS enumerates network interfaces only at participant
        # creation. This container auto-starts at host boot (restart:
        # unless-stopped), usually before the per-camera USB-ethernet links
        # exist, so the participant advertises unicast locators the cameras
        # can't route to and never receives a single message. That state
        # does NOT self-heal (observed fully stale >15min while a fresh
        # `ros2 topic list` in the same container saw every topic
        # instantly); the only fix is recreating the participant, i.e.
        # restarting this process. run_dashboard.sh has the same check, but
        # only runs when someone invokes the script -- this covers headless
        # boots too.
        #
        # Exit condition (deliberately conservative so it can never fire
        # during normal operation): this process has NEVER received any ROS
        # message, yet a camera link has been up continuously for 60s. The
        # grace period rides out a freshly plugged camera still booting its
        # own ROS stack (~30-60s); the never-received-anything condition
        # means recording or live use can't be interrupted -- there is
        # nothing flowing to interrupt. The thread retires for good on the
        # first message, so its steady-state cost is zero.
        link_grace_sec = 60.0
        poll_sec = 5.0
        link_up_since: Optional[float] = None
        while True:
            time.sleep(poll_sec)
            if self._any_ros_data_received():
                return
            if not self._camera_link_up():
                link_up_since = None
                continue
            now = time.monotonic()
            if link_up_since is None:
                link_up_since = now
                continue
            if now - link_up_since < link_grace_sec:
                continue
            if os.path.exists("/.dockerenv"):
                self.get_logger().error(
                    "Camera link up for 60s but no ROS data ever received -- DDS participant "
                    "likely predates the camera links. Exiting so the container restart policy "
                    "recreates the participant with the links present."
                )
                os._exit(1)
            # Outside docker there is no restart policy to catch the exit;
            # warn instead, and re-arm so the warning repeats every grace
            # period rather than spamming every poll.
            self.get_logger().warning(
                "Camera link up for 60s but no ROS data ever received -- DDS participant "
                "likely predates the camera links. Restart this process to recover."
            )
            link_up_since = now

    def _in_bag_range(self, stamp_ns: int) -> bool:
        # Insight cameras stamp headers from the device boot clock (minutes
        # since power-on, not epoch), so those stamps carry no date and can
        # never match the bag metadata's epoch-based range -- comparing them
        # dropped every replayed frame and froze the panels for the whole
        # playback. A stamp before ~2001 is boot-relative: let it through
        # (live and replayed frames may interleave while both are flowing,
        # which beats showing nothing). Same reasoning when the bag has no
        # readable time range at all.
        if stamp_ns < 946_684_800 * 1_000_000_000:  # before 2000: uptime, not a date
            return True
        r = self._bag_time_range
        if r is None:
            return True
        return r[0] <= stamp_ns <= r[1]

    def _make_pose_callback(self, pose_name: str):
        def callback(msg: PoseStamped) -> None:
            stamp_ns = self._stamp_to_ns(msg.header.stamp)
            if self._playback_mode and not self._in_bag_range(stamp_ns):
                return
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
                    writer = InProcessBagWriter(
                        output_path,
                        storage_config_uri=str(STORAGE_CONFIG_PATH) if STORAGE_CONFIG_PATH.is_file() else "",
                    )
                    writers_by_path[output_path] = writer
                writer_by_topic[topic] = writer
            self._recording_writers = writers_by_path
            self._recording_writer_by_topic = writer_by_topic

    def stop_image_recording(self) -> Dict[str, object]:
        with self._recording_writer_lock:
            writers = self._recording_writers
            self._recording_writers = {}
            self._recording_writer_by_topic = {}
        dropped = 0
        for writer in writers.values():
            writer.close()
            dropped += writer.dropped_count
        return {"dropped": dropped}

    def _feed_recording_writer(self, topic: str, msg: object) -> None:
        writer = self._recording_writer_by_topic.get(topic)
        if writer is None:
            return
        writer.write(topic, msg, self.get_clock().now().nanoseconds)

    def _make_dashboard_image_callback(self, camera_name: str, topic_type: str, also_alignment: bool = False):
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
        def callback(msg) -> None:
            if self._playback_mode:
                stamp_ns = self._stamp_to_ns(msg.header.stamp)
                if not self._in_bag_range(stamp_ns):
                    return
            else:
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
                frame = self._encode_dashboard_frame(camera_name, topic_type, msg, display_image)
                if frame is None:
                    continue
                with self.camera_frame_lock:
                    self.latest_camera_frames[camera_name] = frame
                    self.camera_frame_times[camera_name].append(frame.received_monotonic)
                if self.webrtc_streams is not None:
                    self.webrtc_streams.push_ros_frame(camera_name, topic_type, msg, frame)
            except Exception as exc:  # noqa: BLE001 - a bad frame must not kill the worker
                self.get_logger().warning(f"frame worker {camera_name}: {exc}")

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
            if camera_name in self.hand_overlay_enabled:
                # Decode/draw/re-encode -- same per-frame cost the reference
                # PC tool (visualize_hand_landmarks.py) pays to bake the
                # skeleton into the pixels it displays/records. Only paid by
                # cameras with the Settings toggle on; everyone else keeps
                # the cheap passthrough (no decode) below.
                composited = self.compose_hand_overlay_jpeg(camera_name, data)
                if composited is not None:
                    data = composited
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
            self.raw_traces[pose_name].append(pose_sample.position)

    def clear_traces(self) -> None:
        with self.pose_lock:
            for name in self.raw_traces:
                self.raw_traces[name].clear()
            for name in self.last_pose_received_time:
                self.last_pose_received_time[name] = 0.0

    def set_playback_mode(self, enabled: bool,
                          bag_time_range: Optional[Tuple[int, int]] = None) -> None:
        self._playback_mode = enabled
        self._bag_time_range = bag_time_range if enabled else None
        self.get_logger().info(
            f"Playback mode {'ON' if enabled else 'OFF'}"
            + (f" range=[{bag_time_range[0]}, {bag_time_range[1]}]" if bag_time_range else "")
        )

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
                poses.append(
                    {
                        "name": pose.name,
                        "role": pose.teleop_role,
                        "visible": visible,
                        "position": position,
                        "quaternion_xyzw": quaternion,
                        "trace": [
                            [round(float(sample[0]), 4), round(float(sample[1]), 4), round(float(sample[2]), 4)]
                            for sample in self.transformed_trace(pose.name)
                        ],
                        "avatar_model": pose.avatar_model,
                        "avatar_scale": pose.avatar_scale,
                        "avatar_rotation_deg_xyz": [float(value) for value in pose.avatar_rotation_deg_xyz],
                        "avatar_offset_xyz": [float(value) for value in pose.avatar_offset_xyz],
                        "gripper_opening": self.gripper_opening_percent(pose.name),
                    }
                )
        return {
            "type": "pose_update",
            "timestamp_ms": int(time.time() * 1000),
            "fake_pose": self.fake_pose,
            "playback_mode": self._playback_mode,
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
                        "webrtc_available": self.webrtc_streams is not None,
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


@dataclass
class _ScoringJob:
    bag_name: str
    bag_path: str
    topic: str          # empty = auto-discover all; non-empty = score only this topic
    status: str         # "running" | "done" | "error"
    result: Optional[Dict] = None
    error: Optional[str] = None
    started_at: float = 0.0
    finished_at: float = 0.0
    current_topic: str = ""  # which topic is being scored right now


class ScoringManager:
    _TRAJ_SCORE = Path(__file__).with_name("traj_score.py")

    def __init__(self, rosbag_root: Path, results_root: Path) -> None:
        self.rosbag_root = rosbag_root
        self.results_root = results_root
        self._lock = threading.Lock()
        self._current_job: Optional[_ScoringJob] = None

    @property
    def status(self) -> Dict:
        with self._lock:
            if self._current_job is None:
                return {"status": "idle"}
            job = self._current_job
            payload: Dict = {
                "status": job.status,
                "bag_name": job.bag_name,
                "topic": job.current_topic or job.topic,
                "started_at": job.started_at,
            }
            if job.result is not None:
                payload["result"] = job.result
            if job.error is not None:
                payload["error"] = job.error
            if job.finished_at:
                payload["finished_at"] = job.finished_at
            return payload

    def run(self, bag_name: str, topic: str = "") -> bool:
        """Start a new scoring job. Returns False if a job is already running."""
        with self._lock:
            if self._current_job and self._current_job.status == "running":
                return False
            bag_path = str((self.rosbag_root / bag_name).resolve())
            job = _ScoringJob(
                bag_name=bag_name,
                bag_path=bag_path,
                topic=topic,
                status="running",
                started_at=time.monotonic(),
            )
            self._current_job = job
        threading.Thread(target=self._worker, args=(job,), daemon=True, name="traj_score").start()
        return True

    def _worker(self, job: _ScoringJob) -> None:
        try:
            if job.topic:
                topics = [job.topic]
            else:
                topics = self._find_cov_topics(job.bag_path)
            if not topics:
                raise RuntimeError(
                    "No PoseWithCovarianceStamped topic found in bag. Specify the topic explicitly."
                )

            scores_dir = self.results_root / "scores"
            scores_dir.mkdir(parents=True, exist_ok=True)

            cameras = []
            for topic in topics:
                with self._lock:
                    job.current_topic = topic

                safe_name = topic.replace("/", "_").strip("_")
                output_json = scores_dir / f"{job.bag_name}__{safe_name}.json"

                cmd = [
                    "/usr/bin/python3",
                    str(self._TRAJ_SCORE),
                    job.bag_path,
                    "--topic", topic,
                    "--json", str(output_json),
                ]
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=300, env=os.environ.copy()
                )
                if proc.returncode != 0:
                    cameras.append({
                        "topic": topic,
                        "error": (proc.stderr or proc.stdout).strip(),
                    })
                    continue

                with output_json.open(encoding="utf-8") as fh:
                    data = json.load(fh)
                cameras.append(data)

            summary = {"cameras": cameras}
            summary_json = scores_dir / f"{job.bag_name}.json"
            with summary_json.open("w", encoding="utf-8") as fh:
                json.dump(summary, fh, indent=2)

            with self._lock:
                job.status = "done"
                job.current_topic = ""
                job.result = summary
                job.finished_at = time.monotonic()

        except Exception as exc:
            with self._lock:
                job.status = "error"
                job.error = str(exc)
                job.finished_at = time.monotonic()

    def _find_cov_topics(self, bag_path: str) -> List[str]:
        """Scan bag topics and return all PoseWithCovarianceStamped topics."""
        cmd = ["/usr/bin/python3", str(self._TRAJ_SCORE), bag_path, "--list-topics"]
        found = []
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30, env=os.environ.copy()
            )
            for line in proc.stdout.splitlines():
                stripped = line.strip()
                if "PoseWithCovarianceStamped" in stripped:
                    topic = stripped.split("[")[0].strip()
                    if topic.startswith("/"):
                        found.append(topic)
        except Exception:
            pass
        return found


# Settings-page-editable subsets of config/board_calibration.json and
# config/post_processing.json. Both files are only read once at process
# startup (camera_setup.load_setup / post_processing.load_post_processing_config),
# so writes here take effect only after a restart -- see
# WebDashboardServer._handle_settings_restart.
_BOARD_CALIBRATION_FIELDS: Dict[str, type] = {
    "marker_length_m": float,
    "marker_separation_m": float,
    "board_rows": int,
    "board_cols": int,
    "max_translation_std_m": float,
    "max_rotation_std_deg": float,
}
_ROSBAG_SYNC_FIELDS: Dict[str, type] = {
    "sync_rosbag_to_host": bool,
    "host_rosbag_sync_dir": str,
    "host_rosbag_sync_ssh_target": str,
}


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
        self.scoring_manager = ScoringManager(
            rosbag_root=recording_manager.rosbag_root,
            results_root=self.results_root,
        )
        self.playback_manager = PlaybackManager(
            rosbag_root=recording_manager.rosbag_root,
            ros_domain_id=recording_manager.ros_domain_id,
            on_stopped=self._on_playback_finished,
        )
        _pipeline_script = (
            Path(__file__).resolve().parents[2]
            / "looper-vio-colmap-handoff"
            / "scripts"
            / "run_pipeline_from_rosbag.py"
        )
        self.optimization_manager = OptimizationManager(
            project_root=Path(__file__).resolve().parents[1],
            pipeline_script=_pipeline_script,
        )
        self._clients: Set[web.WebSocketResponse] = set()
        self._image_capabilities_cache: Optional[Dict[str, object]] = None
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
        app.router.add_get("/ws/webrtc", self._handle_webrtc_ws)
        app.router.add_get("/healthz", self._handle_healthz)
        app.router.add_get("/api/alignment", self._handle_alignment_snapshot)
        app.router.add_post("/api/alignment/start", self._handle_alignment_start)
        app.router.add_post("/api/alignment/stop", self._handle_alignment_stop)
        app.router.add_get("/api/cameras", self._handle_camera_snapshot)
        app.router.add_get("/api/cameras/{camera_name}/frame", self._handle_camera_frame)
        app.router.add_get("/api/images/capabilities", self._handle_image_capabilities)
        app.router.add_get("/api/recording/status", self._handle_recording_status)
        app.router.add_get("/api/recording/topics", self._handle_recording_topics)
        app.router.add_post("/api/recording/start", self._handle_recording_start)
        app.router.add_post("/api/recording/stop", self._handle_recording_stop)
        app.router.add_post("/api/recording/sync", self._handle_recording_sync)
        app.router.add_get("/api/rosbags", self._handle_rosbag_list)
        app.router.add_delete("/api/rosbags/{bag_name}", self._handle_rosbag_delete)
        app.router.add_post("/api/integrity/run", self._handle_integrity_run)
        app.router.add_post("/api/scoring/run", self._handle_scoring_run)
        app.router.add_get("/api/scoring/status", self._handle_scoring_status)
        app.router.add_post("/api/playback/start", self._handle_playback_start)
        app.router.add_post("/api/playback/stop", self._handle_playback_stop)
        app.router.add_get("/api/playback/status", self._handle_playback_status)
        app.router.add_post("/api/trajectory/clear", self._handle_trajectory_clear)
        app.router.add_post("/api/optimization/start", self._handle_optimization_start)
        app.router.add_post("/api/optimization/stop", self._handle_optimization_stop)
        app.router.add_get("/api/optimization/status", self._handle_optimization_status)
        app.router.add_get("/api/optimization/trajectories", self._handle_optimization_trajectories)
        app.router.add_get("/api/optimization/runs", self._handle_optimization_runs)
        app.router.add_get("/api/settings", self._handle_settings_get)
        app.router.add_post("/api/settings/avatar-model", self._handle_settings_avatar_model)
        app.router.add_post("/api/settings/gripper-tracking", self._handle_settings_gripper_tracking)
        app.router.add_get("/api/settings/board-calibration", self._handle_settings_board_calibration_get)
        app.router.add_post("/api/settings/board-calibration", self._handle_settings_board_calibration_post)
        app.router.add_get("/api/settings/rosbag-sync", self._handle_settings_rosbag_sync_get)
        app.router.add_post("/api/settings/rosbag-sync", self._handle_settings_rosbag_sync_post)
        app.router.add_post("/api/settings/restart-backend", self._handle_settings_restart)
        app.router.add_post("/api/settings/hand-overlay", self._handle_settings_hand_overlay)
        app.router.add_get("/api/cameras/{camera_name}/hand", self._handle_camera_hand_overlay)
        app.router.add_get("/asset", self._handle_asset)
        if self.web_root and self.web_root.exists():
            app.router.add_get("/", self._handle_index)
            app.router.add_get("/3d", self._handle_index)

            app.router.add_get("/images", self._handle_images_page)
            app.router.add_get("/bags", self._handle_bags_page)
            app.router.add_get("/recording", self._handle_recording_page)
            app.router.add_get("/scoring", self._handle_scoring_page)
            app.router.add_get("/optimization", self._handle_optimization_page)
            app.router.add_get("/settings", self._handle_settings_page)
            static_root = self.web_root / "static"
            if static_root.exists():
                app.router.add_static("/static/", str(static_root), show_index=False)
            runs_root = Path(__file__).resolve().parents[1] / "runs"
            runs_root.mkdir(exist_ok=True)
            app.router.add_static("/optimization-runs/", str(runs_root), show_index=False)
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

    async def _handle_webrtc_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20.0)
        await ws.prepare(request)
        camera_name = request.query.get("camera", "")
        streams = self.node.webrtc_streams
        spec = next((c for c in self.node.cameras if c.name == camera_name), None)
        if streams is None or spec is None:
            await ws.send_json({"type": "webrtc_unavailable"})
            await ws.close()
            return ws
        loop = asyncio.get_running_loop()

        def send_signal(payload: Dict) -> None:
            # Offer/ICE callbacks land on GStreamer threads; hop back onto
            # the server loop for the websocket write.
            try:
                asyncio.run_coroutine_threadsafe(ws.send_json(payload), loop)
            except RuntimeError:
                pass  # loop shutting down; the session is about to close too

        session = streams.create_session(camera_name, spec.topic_type, send_signal)
        try:
            async for message in ws:
                if message.type != web.WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(message.data)
                except json.JSONDecodeError:
                    continue
                kind = data.get("type")
                if kind == "answer":
                    session.set_remote_answer(str(data.get("sdp", "")))
                elif kind == "ice" and data.get("candidate"):
                    session.add_ice_candidate(
                        int(data.get("sdpMLineIndex") or 0), str(data["candidate"])
                    )
        finally:
            streams.close_session(session)
        return ws

    async def _handle_healthz(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "fake_pose": self.node.fake_pose})

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
        payload = self.node.build_camera_payload()
        payload["playback_mode"] = self.playback_manager.status()["state"] == "playing"
        return web.json_response(payload)

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

    async def _handle_camera_hand_overlay(self, request: web.Request) -> web.Response:
        camera_name = request.match_info.get("camera_name", "")
        if camera_name not in self.node.hand_overlay_available:
            raise web.HTTPNotFound(text="no hand-landmark data for this camera")
        if camera_name not in self.node.hand_overlay_enabled:
            return web.json_response({"camera": camera_name, "enabled": False, "hands": []})
        payload = self.node.hand_overlay_payload(camera_name) or {
            "camera": camera_name,
            "hands": [],
            "stale": True,
        }
        payload["enabled"] = True
        return web.json_response(payload)

    async def _handle_settings_hand_overlay(self, request: web.Request) -> web.Response:
        payload = await self._read_json_body(request)
        name = str(payload.get("name", "")).strip()
        if not name or "enabled" not in payload:
            raise ValueError("Fields 'name' and 'enabled' are required.")
        self.node.set_hand_overlay_enabled(name, bool(payload.get("enabled")))
        return web.json_response(self.node.build_settings_payload())

    async def _handle_settings_get(self, _request: web.Request) -> web.Response:
        return web.json_response(self.node.build_settings_payload())

    @staticmethod
    async def _read_json_body(request: web.Request) -> dict:
        if not request.can_read_body:
            return {}
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON body: {exc}") from exc
        if payload is None:
            return {}
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object.")
        return payload

    async def _handle_settings_avatar_model(self, request: web.Request) -> web.Response:
        payload = await self._read_json_body(request)
        name = str(payload.get("name", "")).strip()
        model = str(payload.get("model", "")).strip()
        if not name or not model:
            raise ValueError("Fields 'name' and 'model' are required.")
        self.node.set_pose_avatar_model(name, model)
        return web.json_response(self.node.build_settings_payload())

    async def _handle_settings_gripper_tracking(self, request: web.Request) -> web.Response:
        payload = await self._read_json_body(request)
        name = str(payload.get("name", "")).strip()
        if not name or "enabled" not in payload:
            raise ValueError("Fields 'name' and 'enabled' are required.")
        self.node.set_gripper_tracking_enabled(name, bool(payload.get("enabled")))
        return web.json_response(self.node.build_settings_payload())

    def _read_config_json(self, filename: str) -> Dict[str, object]:
        path = self.project_root / "config" / filename
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_config_fields(
        self, filename: str, fields: Dict[str, type], payload: Dict[str, object]
    ) -> Dict[str, object]:
        data = self._read_config_json(filename)
        for key, kind in fields.items():
            if key not in payload:
                continue
            raw_value = payload[key]
            try:
                value = kind(raw_value) if kind is not bool else bool(raw_value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"'{key}' must be a valid {kind.__name__}") from exc
            if kind in (float, int) and value <= 0:
                raise ValueError(f"'{key}' must be a positive number")
            data[key] = value
        path = self.project_root / "config" / filename
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return {key: data.get(key) for key in fields}

    async def _handle_settings_board_calibration_get(self, _request: web.Request) -> web.Response:
        data = self._read_config_json("board_calibration.json")
        return web.json_response(
            {"values": {key: data.get(key) for key in _BOARD_CALIBRATION_FIELDS}, "restart_required": True}
        )

    async def _handle_settings_board_calibration_post(self, request: web.Request) -> web.Response:
        payload = await self._read_json_body(request)
        values = self._write_config_fields("board_calibration.json", _BOARD_CALIBRATION_FIELDS, payload)
        return web.json_response({"values": values, "restart_required": True})

    async def _handle_settings_rosbag_sync_get(self, _request: web.Request) -> web.Response:
        data = self._read_config_json("post_processing.json")
        return web.json_response(
            {"values": {key: data.get(key) for key in _ROSBAG_SYNC_FIELDS}, "restart_required": True}
        )

    async def _handle_settings_rosbag_sync_post(self, request: web.Request) -> web.Response:
        payload = await self._read_json_body(request)
        values = self._write_config_fields("post_processing.json", _ROSBAG_SYNC_FIELDS, payload)
        return web.json_response({"values": values, "restart_required": True})

    async def _handle_settings_restart(self, _request: web.Request) -> web.Response:
        self.node.get_logger().info(
            "Settings: restart requested from the web UI; exiting so "
            "'restart: unless-stopped' brings the backend back up with reloaded config."
        )

        def _delayed_exit() -> None:
            time.sleep(0.5)
            os._exit(0)

        threading.Thread(target=_delayed_exit, daemon=True).start()
        return web.json_response({"ok": True, "message": "Restarting backend..."})

    async def _handle_image_capabilities(self, _request: web.Request) -> web.Response:
        # Installed GStreamer elements can't change without a restart, and
        # probing them shells out to gst-inspect-1.0 up to 10 times (2s
        # timeout each) -- cache for the process lifetime so only the first
        # Images-page visit pays that.
        if self._image_capabilities_cache is None:
            self._image_capabilities_cache = self._build_image_capabilities()
        return web.json_response(self._image_capabilities_cache)

    def _build_image_capabilities(self) -> Dict[str, object]:
        elements = self._detect_gstreamer_elements(
            [
                "webrtcbin",
                "nice",
                "nvv4l2h264enc",
                "nvv4l2h265enc",
                "nvv4l2decoder",
                "nvjpegenc",
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
        # What the display path actually runs today (JPEG frames over HTTP);
        # the H.264/webrtc entries above are readiness reporting for the
        # planned WebRTC transport, not an active pipeline.
        hw_jpeg = getattr(self.node, "_hw_jpeg", None)
        active_path = "jpeg-hardware-nvjpeg" if hw_jpeg is not None else "jpeg-software"
        notes = []
        if hw_jpeg is not None:
            notes.append("Display frames are encoded on the NVJPEG hardware engine (nvjpegenc).")
        else:
            notes.append("Display frames are encoded in software (cv2); NVJPEG path unavailable.")
        if hardware_encoder:
            notes.append(f"Hardware video encoder available for a future WebRTC path: {hardware_encoder}.")
        else:
            notes.append("No Jetson hardware H.264/H.265 encoder detected on this device.")
        if has_webrtc:
            notes.append("WebRTC transport dependencies are present (not yet wired up).")
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
            "hw_jpeg": {"active": hw_jpeg is not None, **(hw_jpeg.status() if hw_jpeg is not None else {})},
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
        payload = self.recording_manager.status()
        payload["system_load"] = read_system_load()
        return web.json_response(payload)

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
        bag_name = str(payload.get("bag_name", "")).strip() or None
        status = self.recording_manager.start(topics=topics, bag_name=bag_name)
        return web.json_response(status)

    async def _handle_recording_stop(self, _request: web.Request) -> web.Response:
        return web.json_response(self.recording_manager.stop())

    async def _handle_recording_sync(self, _request: web.Request) -> web.Response:
        sync_status = self.recording_manager.sync_recording_to_host()
        payload = self.recording_manager.status()
        payload["sync_status"] = sync_status
        return web.json_response(payload)

    async def _handle_rosbag_list(self, _request: web.Request) -> web.Response:
        loop = asyncio.get_event_loop()
        bags = await loop.run_in_executor(
            None, list_rosbags, self.recording_manager.rosbag_root, self.results_root
        )
        return web.json_response(
            {
                "type": "rosbag_list",
                "rosbag_root": str(self.recording_manager.rosbag_root),
                "results_root": str(self.results_root),
                "bags": bags,
            }
        )

    async def _handle_rosbag_delete(self, request: web.Request) -> web.Response:
        bag_name = request.match_info.get("bag_name", "").strip()
        if not bag_name or "/" in bag_name or bag_name in (".", ".."):
            return web.json_response({"error": "Invalid bag name."}, status=400)
        bag_path = (self.recording_manager.rosbag_root / bag_name).resolve()
        if not bag_path.is_relative_to(self.recording_manager.rosbag_root.resolve()):
            return web.json_response({"error": "Access denied."}, status=403)
        if not bag_path.exists():
            return web.json_response({"error": "Bag not found."}, status=404)
        shutil.rmtree(bag_path)
        return web.json_response({"status": "deleted", "bag_name": bag_name})

    async def _handle_integrity_run(self, request: web.Request) -> web.Response:
        payload = await self._read_json_body(request)
        bag_name = str(payload.get("bag_name", "")).strip()
        if not bag_name or "/" in bag_name or bag_name in (".", ".."):
            return web.json_response({"error": "Invalid bag name."}, status=400)
        bag_path = (self.recording_manager.rosbag_root / bag_name).resolve()
        if not bag_path.is_relative_to(self.recording_manager.rosbag_root.resolve()):
            return web.json_response({"error": "Access denied."}, status=403)
        if not bag_path.exists():
            return web.json_response({"error": "Bag not found."}, status=404)
        loop = asyncio.get_event_loop()
        try:
            # Full-table sqlite scan, a few seconds for a GB-scale bag --
            # off the event loop so camera polling doesn't stutter.
            report = await loop.run_in_executor(None, analyze_bag, bag_path)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=422)
        # Persisted next to scores/optimized so list_rosbags can surface a
        # per-bag integrity badge without re-scanning gigabytes per listing.
        integrity_dir = self.results_root / "integrity"
        integrity_dir.mkdir(parents=True, exist_ok=True)
        (integrity_dir / f"{bag_name}.json").write_text(json.dumps(report, indent=2))
        return web.json_response({"type": "integrity_report", **report})

    async def _handle_scoring_run(self, request: web.Request) -> web.Response:
        if request.can_read_body:
            try:
                body = await request.json()
            except json.JSONDecodeError as exc:
                return web.json_response({"error": f"Invalid JSON: {exc}"}, status=400)
        else:
            body = {}
        if not isinstance(body, dict):
            body = {}
        bag_name = str(body.get("bag_name", "")).strip()
        if not bag_name:
            return web.json_response({"error": "bag_name is required"}, status=400)
        topic = str(body.get("topic", "")).strip()
        started = self.scoring_manager.run(bag_name, topic)
        if not started:
            return web.json_response({"error": "A scoring job is already running."}, status=409)
        return web.json_response({"status": "started", "bag_name": bag_name})

    async def _handle_scoring_status(self, _request: web.Request) -> web.Response:
        return web.json_response(self.scoring_manager.status)

    def _on_playback_finished(self) -> None:
        self.node.set_playback_mode(False)
        self.node.clear_traces()

    async def _handle_playback_start(self, request: web.Request) -> web.Response:
        body = await request.json()
        bag_name = str(body.get("bag_name", "")).strip()
        if not bag_name:
            return web.json_response({"error": "bag_name is required"}, status=400)
        time_range = self.playback_manager.get_bag_time_range(bag_name)
        self.node.set_playback_mode(True, time_range)
        self.node.clear_traces()
        self.playback_manager.start(bag_name, self.recording_manager)
        return web.json_response({"status": "playing", "bag_name": bag_name})

    async def _handle_playback_stop(self, _request: web.Request) -> web.Response:
        self.playback_manager.stop()
        self.node.set_playback_mode(False)
        self.node.clear_traces()
        return web.json_response({"status": "idle"})

    async def _handle_playback_status(self, _request: web.Request) -> web.Response:
        return web.json_response(self.playback_manager.status())

    async def _handle_trajectory_clear(self, _request: web.Request) -> web.Response:
        self.node.clear_traces()
        return web.json_response({"ok": True})

    async def _handle_optimization_start(self, request: web.Request) -> web.Response:
        body = await request.json()
        bag_name = str(body.get("bag_name", "")).strip()
        camera_name = str(body.get("camera_name", "")).strip()
        stream_type = str(body.get("stream_type", "color_compressed")).strip()
        run_name = str(body.get("run_name", "")).strip() or bag_name
        if not bag_name:
            return web.json_response({"error": "bag_name is required"}, status=400)
        if stream_type not in IMAGE_STREAMS:
            return web.json_response(
                {"error": f"Unknown stream_type '{stream_type}'. Valid: {list(IMAGE_STREAMS.keys())}"},
                status=400,
            )
        if camera_name:
            cam = next((c for c in self.node.cameras if c.name == camera_name), None)
            if cam is None:
                available = [c.name for c in self.node.cameras]
                return web.json_response(
                    {"error": f"Camera '{camera_name}' not found. Available: {available}"},
                    status=400,
                )
        else:
            cam = self.node.cameras[0] if self.node.cameras else None
        if cam is None:
            return web.json_response({"error": "No cameras configured"}, status=400)
        from camera_setup import camera_base, image_topic as mk_image_topic
        vio = f"{camera_base(cam.namespace)}/vio_100hz"
        img = mk_image_topic(cam.namespace, stream_type)
        self.optimization_manager.start(bag_name, run_name, vio, img)
        return web.json_response({"status": "running", "run_name": run_name, "camera": cam.name, "stream": stream_type, "image_topic": img})

    async def _handle_optimization_stop(self, _request: web.Request) -> web.Response:
        self.optimization_manager.stop()
        return web.json_response({"status": "idle"})

    async def _handle_optimization_status(self, _request: web.Request) -> web.Response:
        return web.json_response(self.optimization_manager.status())

    async def _handle_optimization_trajectories(self, request: web.Request) -> web.Response:
        run_name = request.rel_url.query.get("run_name", "").strip()
        if not run_name:
            return web.json_response({"error": "run_name required"}, status=400)
        project_root = Path(__file__).resolve().parents[1]
        hz_label = "5"
        vio_path = project_root / "data" / "derived" / run_name / "trajectories" / "vio_100hz.tum"
        colmap_path = project_root / "runs" / run_name / "viz" / f"color_{hz_label}hz_vs_vio100" / "colmap_sim3.tum"
        return web.json_response({
            "vio": _read_tum_points(vio_path),
            "colmap": _read_tum_points(colmap_path),
        })

    async def _handle_optimization_runs(self, _request: web.Request) -> web.Response:
        runs_root = Path(__file__).resolve().parents[1] / "runs"
        entries = []
        if runs_root.exists():
            for run_dir in sorted(runs_root.iterdir()):
                if not run_dir.is_dir():
                    continue
                sim3 = next(run_dir.glob("viz/*/colmap_sim3.tum"), None)
                colmap_tum = next(run_dir.glob("colmap/*/colmap.tum"), None)
                if sim3 or colmap_tum:
                    entries.append({
                        "run_name": run_dir.name,
                        "has_sim3": sim3 is not None,
                    })
        return web.json_response({"runs": entries})

    async def _handle_index(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.web_root / "3d.html")

    async def _handle_recording_page(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.web_root / "recording.html")

    async def _handle_images_page(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.web_root / "images.html")

    async def _handle_bags_page(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.web_root / "bags.html")

    async def _handle_scoring_page(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.web_root / "scoring.html")

    async def _handle_optimization_page(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.web_root / "optimization.html")

    async def _handle_settings_page(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.web_root / "settings.html")

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
        # Only ever serves avatar_model .glb/.gltf files (see model_asset_url),
        # which can be several MB — let the browser cache them across page
        # navigations (each dashboard page, incl. /3d, is a separate full
        # page load, so without this every visit re-downloads the model).
        response = web.FileResponse(candidate)
        response.headers["Cache-Control"] = "public, max-age=3600"
        return response


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
        node.destroy_node()
        with contextlib.suppress(Exception):
            rclpy.shutdown()
        spin_thread.join(timeout=1.0)


if __name__ == "__main__":
    main()
