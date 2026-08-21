"""ROS node coordinating field-capture adapters and domain services."""

import math
import os
import secrets
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

import numpy as np

try:
    import rclpy
    from geometry_msgs.msg import PoseStamped
    from rclpy.callback_groups import ReentrantCallbackGroup
    from rclpy.node import Node
    from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
    from rclpy.qos_event import SubscriptionEventCallbacks
    from sensor_msgs.msg import CompressedImage, Image as RosImage
    from tf2_msgs.msg import TFMessage
    from vision_msgs.msg import Detection2DArray
except Exception:  # pragma: no cover - fake mode can run without ROS imports
    rclpy = None
    PoseStamped = None
    ReentrantCallbackGroup = None
    Node = object
    QoSProfile = None
    ReliabilityPolicy = None
    HistoryPolicy = None
    DurabilityPolicy = None
    SubscriptionEventCallbacks = None
    CompressedImage = None
    RosImage = None
    TFMessage = None
    Detection2DArray = None

from insight_capture.core.config import (
    build_dashboard_config,
    load_setup,
)
from insight_capture.core.paths import runtime_config_path
from insight_capture.perception.gripper.tracking import GripperTrackingMixin
from insight_capture.perception.gripper.overlay import HandOverlayMixin
from insight_capture.media.jpeg import HwJpegCodec
from insight_capture.core import performance as perf_tracker
from insight_capture.runtime.recording import RecordingManager
from insight_capture.runtime.ros.topics import playback_topic

from insight_capture.core.models import CameraFrame, CameraSpec, PoseSample, PoseSpec
from insight_capture.media.image_pipeline import ImagePipeline
from insight_capture.media.preview_manager import PreviewManager
from insight_capture.media.worker_supervisor import WorkerSupervisor
from insight_capture.quality.capture_check import CaptureCheckManager
from insight_capture.runtime.mapping.stream import MappingStream
from insight_capture.runtime.payloads import PayloadBuilder
from insight_capture.runtime.recording.header_audit import RecordingBridge
from insight_capture.runtime.watchdog import ParticipantWatchdog


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


class PoseBridgeNode(GripperTrackingMixin, HandOverlayMixin, Node):
    def __init__(
        self,
        config_path: Path,
        fake_pose: bool = False,
        pose_publish_hz: float = 50.0,
        webrtc_port: int = 8766,
        post_processing_config_path: Optional[Path] = None,
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
        self.project_root = config_path.resolve().parents[1]
        self.post_processing_config_path = (
            Path(post_processing_config_path).resolve()
            if post_processing_config_path is not None
            else runtime_config_path()
        )
        self.fake_pose = bool(fake_pose)
        self.max_points = 300

        raw_config = load_setup(config_path)
        config = build_dashboard_config(raw_config)
        enabled_camera_map = {
            camera["name"]: camera for camera in raw_config.get("cameras", []) if camera.get("enabled", True)
        }
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
        self.cameras: List[CameraSpec] = [
            CameraSpec(
                name=item["name"],
                namespace=enabled_camera_map[item["name"]]["namespace"],
                hand_tracking=bool(item.get("hand_tracking", False)),
                label=item["label"],
                topic=item["topic"],
                topic_type=item["type"],
                rotation_deg=int(item.get("rotation_deg", 0)),
                row=int(item.get("row", 0)),
                column=int(item.get("column", 0)),
            )
            for item in config.get("cameras", [])
        ]
        self.poses: List[PoseSpec] = [
            PoseSpec(
                name=item["name"],
                topic=item["topic"],
                teleop_role=str(item.get("teleop_role", item["name"])),
                avatar_model=item.get("avatar_model"),
                avatar_scale=float(item.get("avatar_scale", 1.0)),
                avatar_rotation_deg_xyz=tuple(float(value) for value in item.get("avatar_rotation_deg_xyz", [0.0, 0.0, 0.0])),
                avatar_offset_xyz=tuple(float(value) for value in item.get("avatar_offset_xyz", [0.0, 0.0, 0.0])),
            )
            for item in config.get("poses", [])
        ]
        # Wired by main() after RecordingManager is constructed.
        self.recording_manager: Optional[RecordingManager] = None
        self._playback_mode: bool = False
        self.runtime_mode = os.environ.get("INSIGHT_MODE", "capture").strip().lower() or "capture"
        # Bounded deques avoid per-message trace slicing.
        self.raw_traces: Dict[str, Deque[Tuple[float, float, float]]] = {
            pose.name: deque(maxlen=self.max_points) for pose in self.poses
        }
        self.raw_trace_sequences: Dict[str, Deque[int]] = {
            pose.name: deque(maxlen=self.max_points) for pose in self.poses
        }
        self.trace_sequences: Dict[str, int] = {pose.name: 0 for pose in self.poses}
        self.trace_generation = 0
        self.latest_pose_sample: Dict[str, Optional[PoseSample]] = {pose.name: None for pose in self.poses}
        self.last_pose_received_time: Dict[str, float] = {pose.name: 0.0 for pose in self.poses}
        pose_topics = {pose.name: pose.topic for pose in self.poses}
        self._native_vio_pose_names = {
            camera.name
            for camera in self.cameras
            if pose_topics.get(camera.name)
            == f"/{camera.namespace}/camera/vio_100hz"
        }
        # Native VIO stays available when a camera is switched to raw-only
        # calibration output, unlike the rectified preview/global pose path.
        self.camera_liveness_times: Dict[str, float] = {
            camera.name: 0.0 for camera in self.cameras
        }
        self.pose_lock = threading.Lock()
        self.camera_frame_lock = threading.Lock()
        self.camera_input_lock = threading.Lock()
        self.latest_camera_frames: Dict[str, Optional[CameraFrame]] = {camera.name: None for camera in self.cameras}
        self.camera_frame_versions: Dict[str, int] = {camera.name: 0 for camera in self.cameras}
        self.camera_input_times: Dict[str, Deque[float]] = {
            camera.name: deque(maxlen=120) for camera in self.cameras
        }
        self.camera_frame_times: Dict[str, Deque[float]] = {camera.name: deque(maxlen=90) for camera in self.cameras}
        self.ros_callback_group = ReentrantCallbackGroup()
        # Unused default QoS waitables can crash this Jetson/rmw_fastrtps executor.
        self.subscription_event_callbacks = SubscriptionEventCallbacks(
            use_default_callbacks=False
        )
        self.dashboard_subscriptions = []
        self._tf_static_message = None
        self._tf_static_publisher = None
        # The native recorder owns writes; these flags enable live header audit.
        self._recording_writer_by_topic: Dict[str, bool] = {}
        self._recording_header_audit: Dict[str, Dict[str, object]] = {}
        self._recording_writer_lock = threading.Lock()
        # GIL-atomic latest-frame slots keep heavy work out of ROS callbacks.
        self._pending_frames: Dict[str, object] = {}
        self._pending_frame_events: Dict[str, threading.Event] = {
            camera.name: threading.Event() for camera in self.cameras
        }
        self._localization_image_publishers: Dict[str, object] = {}
        self._last_localization_image_relay_ns: Dict[str, int] = {}
        self._configure_gripper_tracking(str(self.project_root / "config" / "gripper_calibration.json"))
        self._configure_hand_overlay()
        self._capture_check_manager: Optional[CaptureCheckManager] = None
        # NVJPEG is optional; call sites retain their cv2 fallback.
        self._hw_jpeg = HwJpegCodec.create(log=self.get_logger().info)
        # WebRTC runs out of process to avoid GIL contention with pose broadcasts.
        self.webrtc_port = webrtc_port
        self._webrtc_ipc_path = os.path.join(tempfile.gettempdir(), f"insight_webrtc_{os.getpid()}.sock")
        self._webrtc_authkey = secrets.token_bytes(32)
        self._webrtc_has_sessions: Dict[str, bool] = {}
        self._webrtc_session_fps: Dict[str, int] = {}
        self._next_webrtc_frame_at: Dict[str, float] = {}
        self._pending_webrtc_frames: Dict[str, Tuple[str, int, int, bytes]] = {}
        self._webrtc_frame_event = threading.Event()
        self._webrtc_available_cached = False
        self._webrtc_metrics_lock = threading.Lock()
        self._webrtc_main_metrics: Dict[str, Dict[str, object]] = {
            camera.name: {
                "queued": 0,
                "throttled": 0,
                "replaced": 0,
                "ipc_sent": 0,
                "queued_fps": 0.0,
                "throttled_fps": 0.0,
                "replaced_fps": 0.0,
                "ipc_fps": 0.0,
            }
            for camera in self.cameras
        }
        self._webrtc_worker_stats: Dict[str, Dict[str, object]] = {}
        self._webrtc_browser_stats: Dict[str, Dict[str, object]] = {}
        self._last_webrtc_fallback_jpeg_at: Dict[str, float] = {}
        self._last_recording_preview_at: Dict[str, float] = {}
        self._webrtc_worker_lock = threading.Lock()
        self._webrtc_proc: Optional[subprocess.Popen] = None
        self._preview_manager = PreviewManager(
            self,
            lease_sec=float(os.environ.get("INSIGHT_PREVIEW_LEASE_SEC", "5")),
            idle_stop_sec=float(os.environ.get("INSIGHT_WEBRTC_IDLE_STOP_SEC", "45")),
        )
        threading.Thread(target=self._webrtc_ipc_loop, daemon=True, name="webrtc_ipc").start()
        threading.Thread(target=self._webrtc_healthz_loop, daemon=True, name="webrtc_healthz").start()
        # Hand-overlay JPEG compositing also runs out of process.
        self._hand_overlay_ipc_path = os.path.join(tempfile.gettempdir(), f"insight_hand_overlay_{os.getpid()}.sock")
        self._hand_overlay_authkey = secrets.token_bytes(32)
        self._pending_hand_overlay_frames: Dict[str, Tuple[int, bytes, list]] = {}
        self._hand_overlay_frame_event = threading.Event()
        self._hand_overlay_worker_lock = threading.Lock()
        # Reject only composites older than the last applied result.
        self._hand_overlay_last_applied: Dict[str, int] = {}
        self._hand_overlay_proc: Optional[subprocess.Popen] = None
        threading.Thread(target=self._hand_overlay_ipc_loop, daemon=True, name="hand_overlay_ipc").start()
        self.create_timer(10.0, self._log_perf_summary, callback_group=self.ros_callback_group)
        if self.fake_pose:
            self.create_timer(1.0 / self.pose_publish_hz, self._update_fake_pose, callback_group=self.ros_callback_group)
            self.get_logger().info("Running in fake-pose demo mode")
        else:
            self._mapping_stream.start()
            self._create_tf_static_relay()
            self._create_pose_subscriptions()
            self._create_dashboard_image_subscriptions()
            self._create_hand_overlay_subscriptions()
            threading.Thread(
                target=self._stale_participant_watchdog_loop,
                daemon=True,
                name="stale_dds_watchdog",
            ).start()

    def _create_tf_static_relay(self) -> None:
        """Cache the latched transforms so a paused recorder can receive them."""
        if TFMessage is None:
            return
        qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self._tf_static_publisher = self.create_publisher(TFMessage, "/tf_static", qos)
        subscription = self.create_subscription(
            TFMessage,
            "/tf_static",
            self._cache_tf_static,
            qos,
            callback_group=self.ros_callback_group,
            event_callbacks=self.subscription_event_callbacks,
        )
        self.dashboard_subscriptions.append(subscription)

    def _cache_tf_static(self, message: object) -> None:
        self._tf_static_message = message

    def republish_tf_static(self) -> bool:
        message = self._tf_static_message
        publisher = self._tf_static_publisher
        if message is None or publisher is None:
            return False
        publisher.publish(message)
        return True

    def _create_pose_subscriptions(self) -> None:
        pose_qos = make_qos()
        liveness_qos = make_image_qos(depth=5, reliability="best_effort")
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
                playback_topic(pose.topic),
                self._make_pose_callback(pose.name, is_live=False),
                pose_qos,
                callback_group=self.ros_callback_group,
                event_callbacks=self.subscription_event_callbacks,
            )
            self.dashboard_subscriptions.append(playback_sub)
            self.get_logger().info(f"Trajectory: {pose.name} <- {pose.topic}")

        for camera in self.cameras:
            native_vio_topic = f"/{camera.namespace}/camera/vio_100hz"
            if camera.name in self._native_vio_pose_names:
                continue
            liveness_sub = self.create_subscription(
                PoseStamped,
                native_vio_topic,
                self._make_camera_liveness_callback(camera.name),
                liveness_qos,
                callback_group=self.ros_callback_group,
                event_callbacks=self.subscription_event_callbacks,
            )
            self.dashboard_subscriptions.append(liveness_sub)
            self.get_logger().info(
                f"Camera liveness: {camera.name} <- {native_vio_topic}"
            )

    def _create_dashboard_image_subscriptions(self) -> None:
        # Depth 20 absorbs executor stalls without overwriting recording frames.
        image_qos = make_image_qos(depth=20, reliability=self.image_qos_reliability)
        for camera in self.cameras:
            msg_type = CompressedImage if camera.topic_type == "compressed" else RosImage
            sub = self.create_subscription(
                msg_type,
                camera.topic,
                self._make_dashboard_image_callback(camera.name, camera.topic_type),
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
                playback_topic(camera.topic),
                self._make_dashboard_image_callback(
                    camera.name, camera.topic_type, is_live=False
                ),
                image_qos,
                callback_group=self.ros_callback_group,
                event_callbacks=self.subscription_event_callbacks,
            )
            self.dashboard_subscriptions.append(playback_sub)
            if camera.name in ("insight3_a", "insight3_b") and msg_type is RosImage:
                relay_topic = (
                    f"/insight_mapping/{camera.name}/infra1/image_rect_raw"
                )
                self._localization_image_publishers[camera.name] = (
                    self.create_publisher(
                        RosImage,
                        relay_topic,
                        make_image_qos(depth=1, reliability="best_effort"),
                    )
                )
                self.get_logger().info(
                    f"Localization relay: {camera.name} -> {relay_topic} at 2 Hz"
                )
            threading.Thread(
                target=self._frame_worker_loop,
                args=(camera.name, camera.topic_type),
                daemon=True,
                name=f"frame_worker_{camera.name}",
            ).start()
            self.get_logger().info(f"Images: {camera.name} <- {camera.topic} type={camera.topic_type}")

    def _create_hand_overlay_subscriptions(self) -> None:
        if Detection2DArray is None:
            return
        # Subscribe only configured HandEngine sources; availability becomes
        # true after the first message.
        hand_qos = make_image_qos(depth=5, reliability="best_effort")
        for camera in self.cameras:
            if not camera.hand_tracking:
                continue
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
            # Replay keypoints for gesture/skeleton behavior. Box metadata is
            # live-only and intentionally omitted from rosbag playback.
            kp_playback_sub = self.create_subscription(
                Detection2DArray,
                playback_topic(keypoints_topic),
                lambda msg, name=camera.name: self._on_hand_keypoints(name, msg, is_live=False),
                hand_qos,
                callback_group=self.ros_callback_group,
                event_callbacks=self.subscription_event_callbacks,
            )
            self.dashboard_subscriptions.extend(
                [box_sub, kp_sub, kp_playback_sub]
            )

    def _log_perf_summary(self) -> None:
        snapshot = perf_tracker.snapshot_and_reset()
        if not snapshot["percent_of_one_core"]:
            return
        self.get_logger().info(perf_tracker.format_summary(snapshot))

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
            pose_sample = PoseSample(
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

    def _make_camera_liveness_callback(self, camera_name: str):
        def callback(_msg: PoseStamped) -> None:
            if not self._playback_mode:
                self.camera_liveness_times[camera_name] = time.monotonic()

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
        self, camera_name: str, topic_type: str, is_live: bool = True
    ):
        return self._image_pipeline._make_dashboard_image_callback(
            camera_name, topic_type, is_live=is_live
        )


    def _frame_worker_loop(
        self, camera_name: str, topic_type: str
    ) -> None:
        self._image_pipeline._frame_worker_loop(camera_name, topic_type)


    def _maybe_queue_webrtc_frame(self, camera_name: str, topic_type: str, msg, frame) -> None:
        self._worker_supervisor._maybe_queue_webrtc_frame(camera_name, topic_type, msg, frame)


    def _start_webrtc_worker(self) -> "subprocess.Popen":
        return self._worker_supervisor._start_webrtc_worker()

    def note_viewer_activity(self) -> None:
        self._preview_manager.activity()

    def viewer_connected(self) -> None:
        self._preview_manager.viewer_connected()

    def viewer_disconnected(self) -> None:
        self._preview_manager.viewer_disconnected()

    def preview_requested(self) -> bool:
        return self._preview_manager.requested()

    def preview_status(self) -> Dict[str, object]:
        return {"configured_mode": self.runtime_mode, **self._preview_manager.status()}

    def close(self) -> None:
        """Stop node-owned media resources before ROS node destruction."""

        self._preview_manager.close()
        self.stop_webrtc_worker()
        self.stop_hand_overlay_worker()


    def stop_webrtc_worker(self) -> None:
        self._worker_supervisor.stop_webrtc_worker()


    def _start_hand_overlay_worker(self) -> "subprocess.Popen":
        return self._worker_supervisor._start_hand_overlay_worker()

    def ensure_hand_overlay_worker(self) -> None:
        self._worker_supervisor.ensure_hand_overlay_worker()

    def stop_hand_overlay_worker(self) -> None:
        self._worker_supervisor.stop_hand_overlay_worker()

    def configure_capture_check(
        self, config: Dict[str, object], results_root: Path
    ) -> None:
        self._capture_check_manager = CaptureCheckManager(
            pose_roles={pose.name: pose.teleop_role for pose in self.poses},
            mapping_snapshot=self.build_mapping_payload,
            results_root=results_root,
            config=config,
        )

    def capture_check_status(self, *, bag_name: Optional[str] = None) -> Dict[str, object]:
        manager = self._capture_check_manager
        if manager is None:
            return {"type": "capture_check", "enabled": False, "state": "disabled"}
        return manager.snapshot(bag_name=bag_name)

    def run_capture_check(self, *, bag_name: Optional[str] = None) -> Dict[str, object]:
        manager = self._capture_check_manager
        if manager is None:
            return {"type": "capture_check_result", "state": "disabled"}
        return manager.check(bag_name=bag_name)

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


    def _record_pose_sample(self, pose_name: str, pose_sample: PoseSample) -> None:
        received = time.monotonic()
        if pose_name in self._native_vio_pose_names:
            self.camera_liveness_times[pose_name] = received
        with self.pose_lock:
            self.latest_pose_sample[pose_name] = pose_sample
            self.last_pose_received_time[pose_name] = received
            self.trace_sequences[pose_name] += 1
            self.raw_traces[pose_name].append(pose_sample.position)
            self.raw_trace_sequences[pose_name].append(self.trace_sequences[pose_name])
        capture_check = self._capture_check_manager
        if capture_check is not None:
            capture_check.record_pose(pose_name, pose_sample, received)

    def clear_traces(self) -> None:
        with self.pose_lock:
            for name in self.raw_traces:
                self.raw_traces[name].clear()
                self.raw_trace_sequences[name].clear()
            for name in self.last_pose_received_time:
                self.last_pose_received_time[name] = 0.0
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
