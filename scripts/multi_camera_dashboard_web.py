#!/usr/bin/env python3

import argparse
import contextlib
import math
import os
import secrets
import tempfile
import threading
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

import numpy as np

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
from gripper_tracking import GripperTrackingMixin
from hand_overlay import HandOverlayMixin
from hw_jpeg import HwJpegCodec
from inprocess_bag_writer import InProcessBagWriter
import perf_tracker
from live_alignment import LiveAlignmentMixin
from post_processing import (
    RecordingManager,
    build_default_topics,
    load_post_processing_config,
)
from session_alignment import PoseSample
from dashboard_web import WebDashboardServer, bagplay_topic

from dashboard_runtime import (
    CameraFrame,
    CameraSpec,
    GestureRecordingController,
    ImagePipeline,
    MappingStream,
    ParticipantWatchdog,
    PayloadBuilder,
    PoseSpec,
    RecordingBridge,
    WorkerSupervisor,
)


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
        self._participant_watchdog = ParticipantWatchdog(self)
        self._recording_bridge = RecordingBridge(self)
        self._image_pipeline = ImagePipeline(self)
        self._worker_supervisor = WorkerSupervisor(self)
        self._payload_builder = PayloadBuilder(self)
        self._mapping_stream = MappingStream(self)
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
        # Reliable image QoS prevents whole-frame loss from fragmented UDP samples.
        self.image_qos_reliability = str(trajectory_config.get("image_qos_reliability", "best_effort"))
        self.pose_publish_hz = max(1.0, float(trajectory_config.get("pose_publish_hz", pose_publish_hz)))
        self.display_fps_limit = min(
            120.0,
            max(1.0, float(trajectory_config.get("display_fps_limit", 20.0))),
        )
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

        # Wired by main() after RecordingManager is constructed.
        self.recording_manager: Optional[RecordingManager] = None
        self._playback_mode: bool = False
        # In-memory settings toggle for primitive pose rendering.
        self.stick_figure_mode: bool = False
        # Bounded deques avoid per-message trace slicing.
        self.raw_traces: Dict[str, Deque[Tuple[float, float, float]]] = {
            pose.name: deque(maxlen=self.max_points) for pose in self.poses
        }
        self.raw_trace_sequences: Dict[str, Deque[int]] = {
            pose.name: deque(maxlen=self.max_points) for pose in self.poses
        }
        self.trace_sequences: Dict[str, int] = {pose.name: 0 for pose in self.poses}
        self.trace_generation = 0
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
        # Unused default QoS waitables can crash this Jetson/rmw_fastrtps executor.
        self.subscription_event_callbacks = SubscriptionEventCallbacks(
            use_default_callbacks=False
        )
        self.dashboard_subscriptions = []
        # Reuse image subscriptions and keep one writer per camera to avoid drops.
        self._recording_writers: Dict[str, InProcessBagWriter] = {}
        self._recording_writer_by_topic: Dict[str, InProcessBagWriter] = {}
        # Map boot-relative image stamps once while preserving camera cadence.
        self._recording_timestamp_offsets_ns: Dict[str, Optional[int]] = {}
        self._recording_header_audit: Dict[str, Dict[str, object]] = {}
        self._recording_writer_lock = threading.Lock()
        # GIL-atomic latest-frame slots keep heavy work out of ROS callbacks.
        self._pending_frames: Dict[str, object] = {}
        self._pending_frame_events: Dict[str, threading.Event] = {
            camera.name: threading.Event() for camera in self.cameras
        }
        self._configure_gripper_tracking(str(self.project_root / "config" / "gripper_calibration.json"))
        self._configure_hand_overlay()
        self._gesture_recording_controller: Optional[GestureRecordingController] = None
        # NVJPEG is optional; call sites retain their cv2 fallback.
        self._hw_jpeg = HwJpegCodec.create(log=self.get_logger().info)
        # WebRTC runs out of process to avoid GIL contention with pose broadcasts.
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
        # Hand-overlay JPEG compositing also runs out of process.
        self._hand_overlay_ipc_path = os.path.join(tempfile.gettempdir(), f"insight_hand_overlay_{os.getpid()}.sock")
        self._hand_overlay_authkey = secrets.token_bytes(32)
        self._pending_hand_overlay_frames: Dict[str, Tuple[int, bytes, list]] = {}
        self._hand_overlay_frame_event = threading.Event()
        # Reject only composites older than the last applied result.
        self._hand_overlay_last_applied: Dict[str, int] = {}
        self._hand_overlay_proc = self._start_hand_overlay_worker()
        threading.Thread(target=self._hand_overlay_ipc_loop, daemon=True, name="hand_overlay_ipc").start()
        self.create_timer(10.0, self._log_perf_summary, callback_group=self.ros_callback_group)
        if self.live_alignment_available:
            self._initialize_live_alignment_state()
            if self.world_to_reference:
                self.get_logger().info(
                    "Loaded persisted live alignment state for web dashboard startup"
                )

        if self.fake_pose:
            self.create_timer(1.0 / self.pose_publish_hz, self._update_fake_pose, callback_group=self.ros_callback_group)
            self.get_logger().info("Running in fake-pose demo mode")
        else:
            self._mapping_stream.start()
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
        # Depth 20 absorbs executor stalls without overwriting recording frames.
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
        # Subscribe all cameras; availability becomes true after the first message.
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

            # Reuse the display subscription to avoid duplicate reliable readers.
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
        return self._participant_watchdog._any_ros_data_received()


    @staticmethod
    def _camera_link_up() -> bool:
        return ParticipantWatchdog._camera_link_up()


    def _restart_for_stale_participant(self, reason: str) -> None:
        self._participant_watchdog._restart_for_stale_participant(reason)


    def _recording_active(self) -> bool:
        return self._participant_watchdog._recording_active()


    def _stale_participant_watchdog_loop(self) -> None:
        self._participant_watchdog._stale_participant_watchdog_loop()


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
            # Camera info feeds live alignment and stays inert during playback.
            if self._playback_mode:
                return
            self.live_alignment_camera_matrix[camera_name] = np.array(msg.k, dtype=np.float64).reshape((3, 3))
            self.live_alignment_dist_coeffs[camera_name] = np.array(msg.d, dtype=np.float64).reshape((-1, 1))

        return callback

    def start_image_recording(self, topic_output_paths: Dict[str, str]) -> None:
        self._recording_bridge.start_image_recording(topic_output_paths)


    def stop_image_recording(self) -> Dict[str, object]:
        return self._recording_bridge.stop_image_recording()


    def _finalize_image_header_audit(self) -> Dict[str, object]:
        return self._recording_bridge._finalize_image_header_audit()


    def _feed_recording_writer(self, topic: str, msg: object) -> None:
        self._recording_bridge._feed_recording_writer(topic, msg)


    def _make_dashboard_image_callback(
        self, camera_name: str, topic_type: str, also_alignment: bool = False, is_live: bool = True
    ):
        return self._image_pipeline._make_dashboard_image_callback(
            camera_name, topic_type, also_alignment=also_alignment, is_live=is_live
        )


    def _frame_worker_loop(
        self, camera_name: str, topic_type: str, also_alignment: bool
    ) -> None:
        self._image_pipeline._frame_worker_loop(
            camera_name, topic_type, also_alignment
        )


    def _maybe_queue_webrtc_frame(self, camera_name: str, topic_type: str, msg, frame) -> None:
        self._worker_supervisor._maybe_queue_webrtc_frame(camera_name, topic_type, msg, frame)


    def _start_webrtc_worker(self) -> "subprocess.Popen":
        return self._worker_supervisor._start_webrtc_worker()


    def stop_webrtc_worker(self) -> None:
        self._worker_supervisor.stop_webrtc_worker()


    def _start_hand_overlay_worker(self) -> "subprocess.Popen":
        return self._worker_supervisor._start_hand_overlay_worker()


    def stop_hand_overlay_worker(self) -> None:
        self._worker_supervisor.stop_hand_overlay_worker()

    def configure_gesture_recording(
        self, recording_manager: RecordingManager, config: Dict[str, object]
    ) -> None:
        self.stop_gesture_recording()
        self._gesture_recording_controller = GestureRecordingController(
            recording_manager,
            config,
            self.get_logger().info,
        )

    def _handle_hand_gesture_snapshot(
        self, camera_name: str, hands: List[Dict[str, object]], *, is_live: bool
    ) -> None:
        controller = self._gesture_recording_controller
        if controller is not None:
            controller.handle_snapshot(camera_name, hands, is_live=is_live)

    def gesture_recording_status(
        self, recording_status: Optional[Dict[str, object]] = None
    ) -> Dict[str, object]:
        controller = self._gesture_recording_controller
        if controller is None:
            return {"enabled": False, "state": "disabled", "message": "Gesture recording disabled"}
        return controller.status(recording_status)

    def stop_gesture_recording(self) -> None:
        controller = getattr(self, "_gesture_recording_controller", None)
        if controller is not None:
            controller.close()
            self._gesture_recording_controller = None


    def _dispatch_hand_overlay(self, camera_name: str, version: int, jpeg_bytes: bytes, hands: list) -> None:
        self._worker_supervisor._dispatch_hand_overlay(camera_name, version, jpeg_bytes, hands)


    def _hand_overlay_ipc_loop(self) -> None:
        self._worker_supervisor._hand_overlay_ipc_loop()


    def _apply_composited_hand_overlay(self, camera_name: str, version: int, composited: bytes) -> None:
        self._worker_supervisor._apply_composited_hand_overlay(camera_name, version, composited)


    def _webrtc_ipc_loop(self) -> None:
        self._worker_supervisor._webrtc_ipc_loop()


    def _webrtc_healthz_loop(self) -> None:
        self._worker_supervisor._webrtc_healthz_loop()


    def _encode_dashboard_frame(
        self, camera_name: str, topic_type: str, msg: object, decoded_image: Optional[np.ndarray] = None
    ) -> Optional[CameraFrame]:
        return self._image_pipeline._encode_dashboard_frame(
            camera_name, topic_type, msg, decoded_image
        )


    def _decode_display_image(self, msg: object) -> Optional[np.ndarray]:
        return self._image_pipeline._decode_display_image(msg)


    @staticmethod
    def _jpeg_dimensions(data: bytes) -> Tuple[int, int]:
        return ImagePipeline._jpeg_dimensions(data)


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
            self.trace_sequences[pose_name] += 1
            self.raw_traces[pose_name].append(pose_sample.position)
            self.raw_trace_sequences[pose_name].append(self.trace_sequences[pose_name])

    def clear_traces(self) -> None:
        with self.pose_lock:
            for name in self.raw_traces:
                self.raw_traces[name].clear()
                self.raw_trace_sequences[name].clear()
            for name in self.last_pose_received_time:
                self.last_pose_received_time[name] = 0.0
            self.trace_generation += 1

    def invalidate_trace_snapshots(self) -> None:
        with self.pose_lock:
            self.trace_generation += 1

    def set_playback_mode(self, enabled: bool) -> None:
        # Topic remapping, not boot-relative timestamps, separates playback.
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

    def build_pose_payload(
        self, trace_cursor: Optional[Dict[str, object]] = None
    ) -> Dict[str, object]:
        return self._payload_builder.build_pose_payload(trace_cursor=trace_cursor)

    def build_mapping_payload(self) -> Dict[str, object]:
        return self._mapping_stream.snapshot()

    def reset_mapping(self) -> Dict[str, object]:
        return self._mapping_stream.request_reset()


    def build_camera_payload(self) -> Dict[str, object]:
        return self._payload_builder.build_camera_payload()


    def latest_camera_frame(self, camera_name: str) -> Optional[CameraFrame]:
        return self._payload_builder.latest_camera_frame(camera_name)


    def model_asset_url(self, avatar_model: Optional[str]) -> Optional[str]:
        return self._payload_builder.model_asset_url(avatar_model)


    def build_settings_payload(self) -> Dict[str, object]:
        return self._payload_builder.build_settings_payload()


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
    # Fallback; cameras.json may override this broadcast rate.
    parser.add_argument("--pose-publish-hz", type=float, default=50.0)
    parser.add_argument("--start-alignment", action="store_true")
    parser.add_argument("--post-processing-config", default=str(Path(__file__).resolve().parents[1] / "config" / "post_processing.json"))
    parser.add_argument("--rosbag-dir", "-rosbag-dir", default=None)
    return parser.parse_args()


def _run_executor(executor: "MultiThreadedExecutor", node: "PoseBridgeNode") -> None:
    # Exit on executor failure so Docker can recover a dead ROS callback loop.
    try:
        executor.spin()
    except Exception:
        node.get_logger().fatal(
            "ROS executor thread crashed; exiting so the container restarts.\n"
            + traceback.format_exc()
        )
        os._exit(1)


def main() -> None:
    # Persist native crash and signal thread dumps outside Docker stdout.
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
    # Global mapping/relocalization is now the only spatial alignment source.
    enable_alignment_stream = False
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
    node.configure_gesture_recording(
        recording_manager,
        dict(post_processing_config.get("gesture_recording") or {}),
    )
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
        node.stop_gesture_recording()
        node.stop_webrtc_worker()
        node.stop_hand_overlay_worker()
        node.destroy_node()
        with contextlib.suppress(Exception):
            rclpy.shutdown()
        spin_thread.join(timeout=1.0)


if __name__ == "__main__":
    main()
