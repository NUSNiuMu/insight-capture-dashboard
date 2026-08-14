#!/usr/bin/env python3

"""Build a session-local head-camera sparse stereo map and publish it for RViz."""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import cv2
import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from insight9_mapping_core import (  # noqa: E402
    GlobalLocalizationConfig,
    IpcSuperGlueBackend,
    LandmarkMap,
    LandmarkMapConfig,
    LocalizationConsensus,
    OfficialSuperGlueBackend,
    PoseBuffer,
    PoseSample,
    RelocalizationEkf,
    RelocalizationEkfConfig,
    StereoCalibration,
    StereoPair,
    StereoPairSynchronizer,
    compose_transform,
    left_to_stereo_center,
    localize_features,
    matrix_from_pose,
    matrix_from_transform,
    rotation_distance_deg,
    select_timestamp,
    transform_points,
    triangulate_rectified,
)
from insight9_mapping_core.pose_graph import (  # noqa: E402
    KeyframePoseGraph,
    PoseGraphConfig,
    PoseGraphOptimizationResult,
)

try:
    import rclpy
    from builtin_interfaces.msg import Time as TimeMsg
    from geometry_msgs.msg import PoseStamped, TransformStamped
    from nav_msgs.msg import Path as PathMsg
    from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
    from rclpy.duration import Duration
    from rclpy.executors import MultiThreadedExecutor
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data
    from rclpy.time import Time
    from sensor_msgs.msg import CameraInfo, Image, PointCloud2, PointField
    from sensor_msgs_py import point_cloud2
    from std_msgs.msg import Header, String
    from std_srvs.srv import Empty
    from tf2_ros import Buffer, TransformBroadcaster, TransformListener
except ImportError as exc:  # pragma: no cover - exercised inside the ROS image
    raise SystemExit(f"ROS 2 Python dependencies are unavailable: {exc}") from exc


POINTCLOUD_MIN_PUBLISH_INTERVAL_SEC = 1.0
POINTCLOUD_REFRESH_INTERVAL_SEC = 10.0


def stamp_to_ns(stamp: TimeMsg) -> int:
    return int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)


def ns_to_stamp(stamp_ns: int) -> TimeMsg:
    result = TimeMsg()
    result.sec = int(stamp_ns // 1_000_000_000)
    result.nanosec = int(stamp_ns % 1_000_000_000)
    return result


def image_to_gray(message: Image) -> np.ndarray:
    """Decode common ROS encodings while respecting row stride."""

    height, width, step = int(message.height), int(message.width), int(message.step)
    raw = np.frombuffer(message.data, dtype=np.uint8)
    encoding = message.encoding.lower()
    if encoding in {"mono8", "8uc1"}:
        required = height * step
        if step < width or raw.size < required:
            raise ValueError("invalid mono8 image buffer")
        return raw[:required].reshape(height, step)[:, :width].copy()
    if encoding in {"rgb8", "bgr8"}:
        required = height * step
        if step < width * 3 or raw.size < required:
            raise ValueError(f"invalid {encoding} image buffer")
        color = raw[:required].reshape(height, step)[:, : width * 3].reshape(
            height, width, 3
        )
        conversion = cv2.COLOR_RGB2GRAY if encoding == "rgb8" else cv2.COLOR_BGR2GRAY
        return cv2.cvtColor(color, conversion)
    if encoding == "nv12":
        # SuperPoint consumes luminance, so the padded Y plane is sufficient.
        required = height * step
        if step < width or raw.size < required:
            raise ValueError("invalid NV12 image buffer")
        return raw[:required].reshape(height, step)[:, :width].copy()
    raise ValueError(f"unsupported image encoding: {message.encoding}")


def matrix_to_pose_stamped(
    transform: np.ndarray, stamp_ns: int, frame_id: str
) -> PoseStamped:
    pose = PoseStamped()
    pose.header.stamp = ns_to_stamp(stamp_ns)
    pose.header.frame_id = frame_id
    pose.pose.position.x, pose.pose.position.y, pose.pose.position.z = (
        float(value) for value in transform[:3, 3]
    )
    rotation = transform[:3, :3]
    trace = float(np.trace(rotation))
    if trace > 0.0:
        scale = 2.0 * np.sqrt(trace + 1.0)
        qw = 0.25 * scale
        qx = (rotation[2, 1] - rotation[1, 2]) / scale
        qy = (rotation[0, 2] - rotation[2, 0]) / scale
        qz = (rotation[1, 0] - rotation[0, 1]) / scale
    else:
        diagonal = int(np.argmax(np.diag(rotation)))
        if diagonal == 0:
            scale = 2.0 * np.sqrt(1.0 + rotation[0, 0] - rotation[1, 1] - rotation[2, 2])
            qw = (rotation[2, 1] - rotation[1, 2]) / scale
            qx = 0.25 * scale
            qy = (rotation[0, 1] + rotation[1, 0]) / scale
            qz = (rotation[0, 2] + rotation[2, 0]) / scale
        elif diagonal == 1:
            scale = 2.0 * np.sqrt(1.0 + rotation[1, 1] - rotation[0, 0] - rotation[2, 2])
            qw = (rotation[0, 2] - rotation[2, 0]) / scale
            qx = (rotation[0, 1] + rotation[1, 0]) / scale
            qy = 0.25 * scale
            qz = (rotation[1, 2] + rotation[2, 1]) / scale
        else:
            scale = 2.0 * np.sqrt(1.0 + rotation[2, 2] - rotation[0, 0] - rotation[1, 1])
            qw = (rotation[1, 0] - rotation[0, 1]) / scale
            qx = (rotation[0, 2] + rotation[2, 0]) / scale
            qy = (rotation[1, 2] + rotation[2, 1]) / scale
            qz = 0.25 * scale
    pose.pose.orientation.x = float(qx)
    pose.pose.orientation.y = float(qy)
    pose.pose.orientation.z = float(qz)
    pose.pose.orientation.w = float(qw)
    return pose


def descriptor_cloud(
    header: Header, points: np.ndarray, descriptors: np.ndarray
) -> PointCloud2:
    """Pack XYZ and fixed-length float descriptors into PointCloud2."""

    xyz = np.asarray(points, dtype=np.float32).reshape(-1, 3)
    values = np.asarray(descriptors, dtype=np.float32)
    if values.ndim != 2 or len(values) != len(xyz):
        raise ValueError("descriptor cloud arrays must have matching rows")
    descriptor_dim = values.shape[1] if values.shape[1] else 256
    packed = (
        np.concatenate((xyz, values), axis=1)
        if len(values)
        else np.empty((0, 3 + descriptor_dim), dtype=np.float32)
    )
    message = PointCloud2()
    message.header = header
    message.height = 1
    message.width = len(packed)
    message.fields = [
        PointField(name="x", offset=0, datatype=PointField.FLOAT32, count=1),
        PointField(name="y", offset=4, datatype=PointField.FLOAT32, count=1),
        PointField(name="z", offset=8, datatype=PointField.FLOAT32, count=1),
        PointField(
            name="descriptor",
            offset=12,
            datatype=PointField.FLOAT32,
            count=descriptor_dim,
        ),
    ]
    message.is_bigendian = False
    message.point_step = int((3 + descriptor_dim) * 4)
    message.row_step = int(message.point_step * message.width)
    message.data = packed.tobytes()
    message.is_dense = True
    return message


@dataclass(frozen=True)
class MappingObservation:
    """Compact stereo observation retained for map rebuilding after loops."""

    observation_id: int
    source_keyframe_id: int
    stamp_ns: int
    odom_to_left: np.ndarray
    points_left: np.ndarray
    descriptors: np.ndarray
    scores: np.ndarray


class Insight9SparseMapper(Node):
    """Coordinate lightweight ROS callbacks with a single inference worker."""

    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__("insight9_sparse_mapper")
        self._args = args
        self._map_frame = args.map_frame
        self._camera_frame = args.mapping_camera_frame
        pose_graph_runtime_limits = (
            args.pose_graph_min_interval_sec,
            args.pose_graph_force_translation_m,
            args.pose_graph_force_rotation_deg,
        )
        if any(
            not np.isfinite(value) or value <= 0.0
            for value in pose_graph_runtime_limits
        ):
            raise ValueError("pose graph scheduling thresholds must be positive")
        if (
            not np.isfinite(args.stationary_observation_interval_sec)
            or args.stationary_observation_interval_sec <= 0.0
        ):
            raise ValueError("stationary observation interval must be positive")
        if args.stationary_bootstrap_min_points < 0:
            raise ValueError("stationary bootstrap point target cannot be negative")
        if args.stationary_bootstrap_max_observations < args.confirmation_observations:
            raise ValueError(
                "stationary bootstrap observation limit must cover confirmation"
            )
        self._pose_buffer = PoseBuffer(max_bracket_gap_ns=50_000_000)
        self._stereo_sync: StereoPairSynchronizer[Image] = StereoPairSynchronizer(
            tolerance_ns=int(args.stereo_tolerance_ms * 1_000_000)
        )
        self._landmark_config = LandmarkMapConfig(
            voxel_size_m=args.voxel_size_m,
            confirmation_observations=args.confirmation_observations,
            candidate_ttl_keyframes=args.candidate_ttl_keyframes,
            max_landmarks=args.max_landmarks,
        )
        self._landmarks = LandmarkMap(self._landmark_config)
        self._pose_graph = KeyframePoseGraph(
            PoseGraphConfig(
                odometry_translation_std_m=(
                    args.pose_graph_odometry_translation_std
                ),
                odometry_rotation_std_deg=args.pose_graph_odometry_rotation_std_deg,
                loop_translation_std_m=args.pose_graph_loop_translation_std,
                loop_rotation_std_deg=args.pose_graph_loop_rotation_std_deg,
                robust_delta=args.pose_graph_robust_delta,
                max_iterations=args.pose_graph_max_iterations,
                max_keyframes=args.pose_graph_max_keyframes,
            )
        )
        self._graph_lock = threading.RLock()
        self._mapping_observations: list[MappingObservation] = []
        self._pose_graph_active = bool(args.pose_graph_enabled)
        self._pose_graph_pending = False
        self._pose_graph_optimization_count = 0
        self._last_pose_graph_optimization_monotonic = float("-inf")
        self._keyframe_storage_bytes = 0
        self._session_generation = 0
        self._loop_config = GlobalLocalizationConfig(
            ratio_test=args.loop_ratio_test,
            min_similarity=args.loop_min_similarity,
            min_matches=args.loop_min_matches,
            min_inliers=args.loop_min_inliers,
            min_inlier_ratio=args.loop_min_inlier_ratio,
            max_reprojection_error_px=args.loop_max_reprojection_error_px,
            min_grid_cells=args.loop_min_grid_cells,
            confirmation_frames=args.loop_confirmation_frames,
            confirmation_translation_m=args.loop_confirmation_translation_m,
            confirmation_rotation_deg=args.loop_confirmation_rotation_deg,
        )
        self._loop_consensus = LocalizationConsensus(self._loop_config)
        self._loop_pose_filter = RelocalizationEkf(
            RelocalizationEkfConfig(
                process_translation_std_m_sqrt_s=args.loop_process_translation_std,
                process_rotation_std_deg_sqrt_s=args.loop_process_rotation_std_deg,
                measurement_translation_std_m=args.loop_measurement_translation_std,
                measurement_rotation_std_deg=args.loop_measurement_rotation_std_deg,
                correction_time_constant_sec=args.loop_correction_time_constant_sec,
            )
        )
        self._loop_pose_filter.observe(np.eye(4, dtype=np.float64))
        self._loop_lock = threading.Lock()
        self._last_loop_measurement: Optional[np.ndarray] = None
        self._loop_closure_count = 0
        self._last_vio_stamp_ns = 0
        self._last_input_vio_stamp_ns = -1
        self._next_vio_buffer_stamp_ns = 0
        self._next_vio_publish_stamp_ns = 0
        self._vio_rate_lock = threading.Lock()
        if args.backend == "ipc":
            self._matcher = IpcSuperGlueBackend(Path(args.inference_socket))
        else:
            self._matcher = OfficialSuperGlueBackend(
                Path(args.superglue_checkout),
                weights=args.superglue_weights,
                max_keypoints=args.max_keypoints,
                keypoint_threshold=args.keypoint_threshold,
                match_threshold=args.match_threshold,
                device=args.device,
            )
        self._calibration_lock = threading.Lock()
        self._left_info: Optional[CameraInfo] = None
        self._right_info: Optional[CameraInfo] = None
        self._calibration: Optional[StereoCalibration] = None
        self._imu_to_left: Optional[np.ndarray] = None
        self._imu_to_center: Optional[np.ndarray] = None
        self._tf_buffer = Buffer(cache_time=Duration(seconds=10.0))
        self._tf_listener = TransformListener(self._tf_buffer, self)
        self._tf_broadcaster = TransformBroadcaster(self)
        self._work: queue.Queue[StereoPair[Image]] = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._worker = threading.Thread(
            target=self._worker_main, name="superglue-mapping", daemon=True
        )
        self._last_mapping_attempt_monotonic = 0.0
        self._last_pointcloud_publish_monotonic = float("-inf")
        self._last_feature_publish_monotonic = 0.0
        self._pointcloud_dirty = True
        self._feature_map_dirty = True
        self._last_keyframe_transform: Optional[np.ndarray] = None
        self._keyframe_id = 0
        self._observation_id = 0
        self._current_keyframe_observations = 0
        self._last_map_observation_stamp_ns = 0
        self._path = deque(maxlen=args.path_points)
        self._path_lock = threading.Lock()
        self._map_lock = threading.Lock()
        self._last_path_append_ns = 0
        self._last_pose_publish_ns = 0
        self._latest_stats = {"state": "waiting_for_inputs"}
        # Point/descriptor cloud serialization can occupy the default callback
        # group for hundreds of milliseconds. Keep the lightweight VIO relay
        # on its own executor lane so visualization poses never queue behind it.
        self._vio_callback_group = MutuallyExclusiveCallbackGroup()

        self._pointcloud_publisher = (
            self.create_publisher(PointCloud2, "insight9_sparse_map/points", 1)
            if args.publish_debug_topics
            else None
        )
        self._feature_map_publisher = self.create_publisher(
            PointCloud2, "insight9_sparse_map/features", 1
        )
        self._path_publisher = (
            self.create_publisher(PathMsg, "insight9_sparse_map/path", 1)
            if args.publish_debug_topics
            else None
        )
        self._pose_publisher = self.create_publisher(
            PoseStamped, "insight9_sparse_map/pose", 1
        )
        self._status_publisher = self.create_publisher(
            String, "insight9_sparse_map/status", 1
        )
        self._reset_service = self.create_service(
            Empty, "insight9_sparse_map/reset", self._on_reset
        )
        self.create_subscription(
            PoseStamped,
            args.vio_topic,
            self._on_vio,
            qos_profile_sensor_data,
            callback_group=self._vio_callback_group,
        )
        self.create_subscription(
            Image, args.left_image_topic, self._on_left, qos_profile_sensor_data
        )
        self.create_subscription(
            Image, args.right_image_topic, self._on_right, qos_profile_sensor_data
        )
        self.create_subscription(
            CameraInfo, args.left_info_topic, self._on_left_info, qos_profile_sensor_data
        )
        self.create_subscription(
            CameraInfo, args.right_info_topic, self._on_right_info, qos_profile_sensor_data
        )
        self.create_timer(0.5, self._publish_map)
        if args.publish_debug_topics:
            self.create_timer(
                1.0 / max(args.path_publish_hz, 0.1), self._publish_path
            )
        self.create_timer(
            1.0 / max(args.tf_publish_hz, 0.1), self._publish_latest_tf
        )
        self.create_timer(0.5, self._resolve_extrinsic)
        self._worker.start()
        self.get_logger().info(
            "official SuperPoint/SuperGlue validation mapper started; "
            "the licensed model image is internal-validation only"
        )
        if not args.publish_debug_topics:
            self.get_logger().info("debug PointCloud2 and Path topics disabled")

    def _on_reset(self, _request: Empty.Request, response: Empty.Response) -> Empty.Response:
        with self._vio_rate_lock:
            self._last_input_vio_stamp_ns = -1
            self._next_vio_buffer_stamp_ns = 0
            self._next_vio_publish_stamp_ns = 0
        with self._path_lock:
            self._path.clear()
            self._last_path_append_ns = 0
        with self._graph_lock:
            self._reset_loop_closure()
            with self._map_lock:
                self._landmarks.clear()
                self._pointcloud_dirty = True
                self._feature_map_dirty = True
                self._last_pointcloud_publish_monotonic = float("-inf")
                self._last_feature_publish_monotonic = 0.0
            self._last_keyframe_transform = None
            self._keyframe_id = 0
            self._observation_id = 0
            self._current_keyframe_observations = 0
            self._last_map_observation_stamp_ns = 0
            self._last_mapping_attempt_monotonic = 0.0
        self._latest_stats = {"state": "waiting_for_motion", "reset": True}
        self._publish_map()
        self.get_logger().info("Started a new web-requested sparse mapping session")
        return response

    def _reset_loop_closure(self) -> None:
        with self._graph_lock:
            self._pose_graph.clear()
            self._mapping_observations.clear()
            self._pose_graph_active = bool(self._args.pose_graph_enabled)
            self._pose_graph_pending = False
            self._pose_graph_optimization_count = 0
            self._last_pose_graph_optimization_monotonic = float("-inf")
            self._keyframe_storage_bytes = 0
            self._session_generation += 1
            with self._loop_lock:
                self._loop_consensus = LocalizationConsensus(self._loop_config)
                self._loop_pose_filter.reset()
                self._loop_pose_filter.observe(np.eye(4, dtype=np.float64))
                self._last_loop_measurement = None
                self._loop_closure_count = 0
                self._last_vio_stamp_ns = 0

    def _map_to_odom(self, *, smoothed: bool = True) -> np.ndarray:
        with self._loop_lock:
            correction = (
                self._loop_pose_filter.correction
                if smoothed
                else self._loop_pose_filter.estimate
            )
        return np.eye(4, dtype=np.float64) if correction is None else correction

    def destroy_node(self) -> bool:
        self._stop.set()
        try:
            self._work.put_nowait(None)  # type: ignore[arg-type]
        except queue.Full:
            pass
        self._worker.join(timeout=3.0)
        return super().destroy_node()

    def _resolve_extrinsic(self) -> None:
        if self._imu_to_left is not None and self._imu_to_center is not None:
            return
        imu_to_left = self._imu_to_left
        if imu_to_left is None:
            try:
                transform = self._tf_buffer.lookup_transform(
                    self._args.imu_frame,
                    self._args.left_frame,
                    Time(),
                    timeout=Duration(seconds=0.05),
                )
            except Exception:
                return
            value = transform.transform
            imu_to_left = matrix_from_transform(
                (value.translation.x, value.translation.y, value.translation.z),
                (
                    value.rotation.x,
                    value.rotation.y,
                    value.rotation.z,
                    value.rotation.w,
                ),
            )
            self._imu_to_left = imu_to_left
            self.get_logger().info(
                "resolved T_imu_left translation=(%.4f, %.4f, %.4f)"
                % tuple(imu_to_left[:3, 3])
            )
        try:
            transform = self._tf_buffer.lookup_transform(
                self._args.left_frame,
                self._args.right_frame,
                Time(),
                timeout=Duration(seconds=0.05),
            )
        except Exception:
            return
        value = transform.transform
        left_to_right = matrix_from_transform(
            (value.translation.x, value.translation.y, value.translation.z),
            (value.rotation.x, value.rotation.y, value.rotation.z, value.rotation.w),
        )
        self._imu_to_center = compose_transform(
            imu_to_left, left_to_stereo_center(left_to_right)
        )
        baseline_m = float(np.linalg.norm(left_to_right[:3, 3]))
        self.get_logger().info(
            f"resolved {self._args.head_camera_name} stereo center from {baseline_m:.4f} m baseline"
        )

    def _on_vio(self, message: PoseStamped) -> None:
        stamp_ns = stamp_to_ns(message.header.stamp)
        with self._vio_rate_lock:
            input_reset = (
                self._last_input_vio_stamp_ns >= 0
                and stamp_ns < self._last_input_vio_stamp_ns
            )
            self._last_input_vio_stamp_ns = stamp_ns
            if input_reset:
                self._next_vio_buffer_stamp_ns = 0
                self._next_vio_publish_stamp_ns = 0
            buffer_due, self._next_vio_buffer_stamp_ns = select_timestamp(
                stamp_ns,
                self._next_vio_buffer_stamp_ns,
                max(50.0, self._args.pose_publish_hz * 2.0),
            )
            if not buffer_due:
                return
            publish_due, self._next_vio_publish_stamp_ns = select_timestamp(
                stamp_ns,
                self._next_vio_publish_stamp_ns,
                self._args.pose_publish_hz,
            )
        path_due = (
            input_reset
            or stamp_ns - self._last_path_append_ns
            >= self._args.path_interval_ms * 1_000_000
        )
        pose = message.pose
        sample = PoseSample(
            stamp_ns=stamp_ns,
            translation=np.array(
                [pose.position.x, pose.position.y, pose.position.z], dtype=np.float64
            ),
            orientation_xyzw=np.array(
                [
                    pose.orientation.x,
                    pose.orientation.y,
                    pose.orientation.z,
                    pose.orientation.w,
                ],
                dtype=np.float64,
            ),
        )
        try:
            odom_to_imu = matrix_from_pose(sample)
            reset = self._pose_buffer.append(sample)
        except ValueError as exc:
            self.get_logger().warning(f"rejected invalid VIO pose: {exc}")
            return
        if reset:
            with self._path_lock:
                self._path.clear()
                self._last_path_append_ns = 0
            with self._graph_lock:
                self._reset_loop_closure()
                with self._map_lock:
                    self._landmarks.clear()
                    self._pointcloud_dirty = True
                    self._feature_map_dirty = True
                self._last_keyframe_transform = None
                self._keyframe_id = 0
                self._last_pose_publish_ns = 0
            self.get_logger().warning("VIO timestamp reset; cleared session map")
        if self._imu_to_center is None:
            return
        odom_to_center = compose_transform(
            odom_to_imu, self._imu_to_center
        )
        if publish_due:
            with self._loop_lock:
                if self._last_vio_stamp_ns > 0:
                    self._loop_pose_filter.predict(
                        (sample.stamp_ns - self._last_vio_stamp_ns)
                        / 1_000_000_000.0
                    )
                self._last_vio_stamp_ns = sample.stamp_ns
                correction = self._loop_pose_filter.correction
            map_to_center = compose_transform(
                np.eye(4, dtype=np.float64) if correction is None else correction,
                odom_to_center,
            )
            stamped = matrix_to_pose_stamped(
                map_to_center, sample.stamp_ns, self._map_frame
            )
            self._pose_publisher.publish(stamped)
            self._last_pose_publish_ns = sample.stamp_ns
        if path_due:
            with self._path_lock:
                self._path.append((sample.stamp_ns, odom_to_center))
            self._last_path_append_ns = sample.stamp_ns

    def _on_left(self, message: Image) -> None:
        pair = self._stereo_sync.push_left(stamp_to_ns(message.header.stamp), message)
        if pair is not None:
            self._offer_pair(pair)

    def _on_right(self, message: Image) -> None:
        pair = self._stereo_sync.push_right(stamp_to_ns(message.header.stamp), message)
        if pair is not None:
            self._offer_pair(pair)

    def _offer_pair(self, pair: StereoPair[Image]) -> None:
        try:
            self._work.put_nowait(pair)
        except queue.Full:
            try:
                self._work.get_nowait()
            except queue.Empty:
                pass
            try:
                self._work.put_nowait(pair)
            except queue.Full:
                pass

    def _on_left_info(self, message: CameraInfo) -> None:
        with self._calibration_lock:
            self._left_info = message
            self._refresh_calibration()

    def _on_right_info(self, message: CameraInfo) -> None:
        with self._calibration_lock:
            self._right_info = message
            self._refresh_calibration()

    def _refresh_calibration(self) -> None:
        if self._left_info is None or self._right_info is None:
            return
        try:
            calibration = StereoCalibration.from_camera_info(
                self._left_info.p,
                self._right_info.p,
                self._left_info.width,
                self._left_info.height,
            )
        except ValueError as exc:
            self.get_logger().error(f"invalid stereo calibration: {exc}")
            return
        if self._calibration is None:
            self.get_logger().info(
                f"stereo calibration ready: {calibration.width}x{calibration.height}, "
                f"baseline={calibration.baseline_m:.5f} m"
            )
        self._calibration = calibration

    def _worker_main(self) -> None:
        period = 1.0 / max(self._args.mapping_hz, 0.1)
        while not self._stop.is_set():
            try:
                pair = self._work.get(timeout=0.25)
            except queue.Empty:
                continue
            if pair is None:
                break
            now = time.monotonic()
            elapsed = now - self._last_mapping_attempt_monotonic
            if elapsed < period:
                continue
            # Limit attempts, not only successful keyframes. Missing poses and
            # stationary cameras must not turn the 5 Hz mapper into a full-rate
            # retry loop with a pose wait on every stereo pair.
            self._last_mapping_attempt_monotonic = now
            calibration = self._calibration
            imu_to_left = self._imu_to_left
            imu_to_center = self._imu_to_center
            pose = self._pose_buffer.lookup(pair.stamp_ns)
            deadline = time.monotonic() + self._args.pose_wait_ms / 1000.0
            while (
                pose is None
                and calibration is not None
                and imu_to_left is not None
                and time.monotonic() < deadline
                and not self._stop.wait(0.005)
            ):
                pose = self._pose_buffer.lookup(pair.stamp_ns)
            if calibration is None or imu_to_left is None or pose is None:
                self._latest_stats = {
                    "state": "waiting_for_inputs",
                    "calibration_ready": calibration is not None,
                    "extrinsic_ready": imu_to_left is not None,
                    "center_extrinsic_ready": imu_to_center is not None,
                    "pose_ready": pose is not None,
                }
                continue
            odom_to_left = compose_transform(matrix_from_pose(pose), imu_to_left)
            motion_keyframe = self._is_keyframe(odom_to_left)
            stationary_observation = (
                not motion_keyframe
                and self._stationary_observation_due(pair.stamp_ns)
            )
            if not motion_keyframe and not stationary_observation:
                continue
            with self._graph_lock:
                session_generation = self._session_generation
            started = time.perf_counter()
            try:
                left = image_to_gray(pair.left)
                right = image_to_gray(pair.right)
                if left.shape != (calibration.height, calibration.width):
                    raise ValueError(
                        f"left image shape {left.shape} differs from CameraInfo "
                        f"{(calibration.height, calibration.width)}"
                    )
                matches = self._matcher.match(left, right)
                triangulated = triangulate_rectified(
                    matches.left_points,
                    matches.right_points,
                    calibration,
                    max_epipolar_error_px=self._args.max_epipolar_error_px,
                    min_disparity_px=self._args.min_disparity_px,
                    min_depth_m=self._args.min_depth_m,
                    max_depth_m=self._args.max_depth_m,
                    max_reprojection_error_px=self._args.max_reprojection_error_px,
                )
                source = triangulated.source_indices
                source_keyframe_id = (
                    self._keyframe_id + 1
                    if motion_keyframe
                    else self._keyframe_id
                )
                next_observation_id = self._observation_id + 1
                observation = MappingObservation(
                    observation_id=next_observation_id,
                    source_keyframe_id=source_keyframe_id,
                    stamp_ns=pair.stamp_ns,
                    odom_to_left=odom_to_left.copy(),
                    points_left=np.asarray(
                        triangulated.points_left, dtype=np.float32
                    ).copy(),
                    descriptors=np.asarray(
                        matches.descriptors[source], dtype=np.float16
                    ).copy(),
                    scores=np.asarray(matches.scores[source], dtype=np.float16).copy(),
                )
                with self._graph_lock:
                    if session_generation != self._session_generation:
                        continue
                    graph_target_id: Optional[int] = None
                    if (
                        motion_keyframe
                        and self._pose_graph_active
                        and self._pose_graph.full
                    ):
                        self._pose_graph_active = False
                        self.get_logger().warning(
                            "pose graph reached %d keyframes; continuing with "
                            "the existing global correction without further graph updates"
                            % self._pose_graph.keyframe_count
                        )
                    if motion_keyframe and self._pose_graph_active:
                        initial_map_pose = compose_transform(
                            self._map_to_odom(smoothed=False), odom_to_left
                        )
                        map_to_left = self._pose_graph.add_keyframe(
                            source_keyframe_id,
                            pair.stamp_ns,
                            odom_to_left,
                            initial_map_pose=initial_map_pose,
                        )
                        graph_target_id = source_keyframe_id
                    elif motion_keyframe:
                        map_to_left = compose_transform(
                            self._map_to_odom(smoothed=False), odom_to_left
                        )
                    else:
                        graph_correction = (
                            self._pose_graph.correction_at(pair.stamp_ns)
                            if self._pose_graph.keyframe_count > 0
                            else None
                        )
                        map_to_left = compose_transform(
                            self._map_to_odom(smoothed=False)
                            if graph_correction is None
                            else graph_correction,
                            odom_to_left,
                        )
                    if self._pose_graph_active:
                        self._mapping_observations.append(observation)
                        self._keyframe_storage_bytes += sum(
                            value.nbytes
                            for value in (
                                observation.odom_to_left,
                                observation.points_left,
                                observation.descriptors,
                                observation.scores,
                            )
                        )
                    self._observation_id = next_observation_id
                    self._last_map_observation_stamp_ns = pair.stamp_ns
                    if motion_keyframe:
                        # Retention above is durable even if a later loop solve
                        # fails; advance IDs before running loop closure.
                        self._keyframe_id = source_keyframe_id
                        self._current_keyframe_observations = 1
                        self._last_keyframe_transform = odom_to_left
                        loop_diagnostics, graph_optimized = self._detect_loop_closure(
                            source_keyframe_id,
                            matches.left_points,
                            matches.descriptors,
                            calibration,
                            odom_to_left,
                            left.shape,
                            graph_target_id=graph_target_id,
                        )
                    else:
                        self._current_keyframe_observations += 1
                        loop_diagnostics = {
                            "loop_checked": False,
                            "loop_closures": self._loop_closure_count,
                            "pose_graph_optimized": False,
                        }
                        graph_optimized = False
                    map_rebuild_ms = 0.0
                    if graph_optimized:
                        rebuild_started = time.perf_counter()
                        update = self._rebuild_landmarks_from_observations()
                        map_rebuild_ms = (
                            time.perf_counter() - rebuild_started
                        ) * 1000.0
                    else:
                        map_points = transform_points(
                            map_to_left, triangulated.points_left
                        )
                        with self._map_lock:
                            update = self._landmarks.update(
                                source_keyframe_id,
                                map_points,
                                observation_id=next_observation_id,
                                descriptors=matches.descriptors[source],
                                scores=matches.scores[source],
                            )
                            self._pointcloud_dirty = True
                            self._feature_map_dirty = True
                self._latest_stats = {
                    "state": "mapping",
                    "keyframe": self._keyframe_id,
                    "observation": self._observation_id,
                    "stationary_observation": stationary_observation,
                    "keyframe_observations": self._current_keyframe_observations,
                    "detected_left": matches.detected_left,
                    "detected_right": matches.detected_right,
                    "stereo_matches": len(matches.left_points),
                    "triangulated": len(triangulated.points_left),
                    "stereo_delta_ms": round(
                        abs(pair.left_stamp_ns - pair.right_stamp_ns) / 1_000_000.0,
                        3,
                    ),
                    "median_disparity_px": (
                        round(float(np.median(triangulated.disparity_px)), 3)
                        if len(triangulated.disparity_px)
                        else None
                    ),
                    "median_reprojection_error_px": (
                        round(float(np.median(triangulated.reprojection_error_px)), 3)
                        if len(triangulated.reprojection_error_px)
                        else None
                    ),
                    "promoted": update.promoted,
                    "confirmed": update.confirmed_total,
                    "candidates": update.candidates_total,
                    "pose_graph_active": self._pose_graph_active,
                    "pose_graph_keyframes": self._pose_graph.keyframe_count,
                    "pose_graph_edges": self._pose_graph.edge_count,
                    "pose_graph_loop_edges": self._pose_graph.loop_edge_count,
                    "pose_graph_pending": self._pose_graph_pending,
                    "pose_graph_optimizations": self._pose_graph_optimization_count,
                    "pose_graph_observation_mb": round(
                        self._keyframe_storage_bytes / 1_000_000.0, 2
                    ),
                    "map_rebuild_ms": round(map_rebuild_ms, 1),
                    "backend_inference_ms": matches.backend_inference_ms,
                    "inference_and_geometry_ms": round(
                        (time.perf_counter() - started) * 1000.0, 1
                    ),
                    **loop_diagnostics,
                }
            except Exception as exc:
                self._latest_stats = {"state": "error", "error": str(exc)}
                self.get_logger().error(f"mapping frame failed: {exc}")

    def _detect_loop_closure(
        self,
        keyframe_id: int,
        keypoints: np.ndarray,
        descriptors: np.ndarray,
        calibration: StereoCalibration,
        odom_to_left: np.ndarray,
        image_shape: tuple[int, int],
        *,
        graph_target_id: Optional[int],
    ) -> tuple[dict[str, object], bool]:
        diagnostics: dict[str, object] = {
            "loop_checked": False,
            "loop_closures": self._loop_closure_count,
            "pose_graph_optimized": False,
        }
        if (
            graph_target_id is not None
            and self._pose_graph_pending
            and time.monotonic() - self._last_pose_graph_optimization_monotonic
            >= self._args.pose_graph_min_interval_sec
        ):
            result = self._optimize_pose_graph(
                diagnostics, trigger="deferred_loop_edge"
            )
            if result.optimized:
                correction = self._pose_graph.correction_for_keyframe(
                    graph_target_id
                )
                with self._loop_lock:
                    self._loop_pose_filter.observe(correction)
                self.get_logger().info(
                    "optimized deferred head-camera pose graph: %.1f ms, "
                    "maximum correction %.3f m / %.2f deg"
                    % (
                        result.elapsed_ms,
                        result.max_translation_correction_m,
                        result.max_rotation_correction_deg,
                    )
                )
                return diagnostics, True
        if (
            not self._args.loop_closure_enabled
            or keyframe_id % max(1, self._args.loop_check_interval_keyframes) != 0
        ):
            return diagnostics, False

        historical_cutoff = keyframe_id - self._args.loop_exclude_recent_keyframes
        with self._map_lock:
            map_points, map_descriptors = self._landmarks.descriptors(
                max_source_keyframe=historical_cutoff
            )
        diagnostics.update(
            {
                "loop_checked": True,
                "loop_historical_features": int(len(map_points)),
                "loop_excluded_after_keyframe": int(historical_cutoff),
            }
        )
        if len(map_points) < self._args.loop_min_map_features:
            diagnostics["loop_rejection"] = "insufficient_historical_map"
            return diagnostics, False

        candidate, localization = localize_features(
            keypoints,
            descriptors,
            map_points,
            map_descriptors,
            calibration.left_projection[:, :3],
            odom_to_left,
            image_shape,
            self._loop_config,
            # LandmarkMap normalizes every descriptor on insert and merge.
            map_descriptors_normalized=True,
        )
        measurement: Optional[np.ndarray] = None
        with self._loop_lock:
            transition = self._loop_consensus.observe(candidate)
            consensus_measurement = self._loop_consensus.correction
            measurement_changed = (
                consensus_measurement is not None
                and (
                    self._last_loop_measurement is None
                    or not np.array_equal(
                        consensus_measurement, self._last_loop_measurement
                    )
                )
            )
            if measurement_changed:
                measurement = consensus_measurement.copy()
                self._last_loop_measurement = measurement.copy()
                self._loop_closure_count += 1
            loop_count = self._loop_closure_count

        graph_optimized = False
        applied_measurement = measurement
        if measurement is not None and graph_target_id is not None:
            accepted_map_pose = compose_transform(measurement, odom_to_left)
            current_graph_pose = self._pose_graph.pose(graph_target_id)
            # Keep the published pose and landmark map in the same geometry
            # while a newly accepted loop edge waits for the rate-limited
            # optimizer. The accepted PnP measurement remains in the graph.
            applied_measurement = self._pose_graph.correction_for_keyframe(
                graph_target_id
            )
            graph_disagreement_translation = float(
                np.linalg.norm(
                    accepted_map_pose[:3, 3] - current_graph_pose[:3, 3]
                )
            )
            graph_disagreement_rotation = rotation_distance_deg(
                current_graph_pose, accepted_map_pose
            )
            self._pose_graph.add_global_loop_edge(
                graph_target_id, accepted_map_pose
            )
            self._pose_graph_pending = True
            elapsed_since_optimization = (
                time.monotonic() - self._last_pose_graph_optimization_monotonic
            )
            optimize_due = (
                self._pose_graph_optimization_count == 0
                or elapsed_since_optimization
                >= self._args.pose_graph_min_interval_sec
                or graph_disagreement_translation
                >= self._args.pose_graph_force_translation_m
                or graph_disagreement_rotation
                >= self._args.pose_graph_force_rotation_deg
                or self._pose_graph.full
            )
            diagnostics.update(
                {
                    "pose_graph_optimize_due": optimize_due,
                    "pose_graph_disagreement_translation_m": round(
                        graph_disagreement_translation, 4
                    ),
                    "pose_graph_disagreement_rotation_deg": round(
                        graph_disagreement_rotation, 3
                    ),
                }
            )
            if optimize_due:
                result = self._optimize_pose_graph(
                    diagnostics, trigger="accepted_loop"
                )
                graph_optimized = result.optimized
                if graph_optimized:
                    applied_measurement = self._pose_graph.correction_for_keyframe(
                        graph_target_id
                    )

        if applied_measurement is not None:
            with self._loop_lock:
                self._loop_pose_filter.observe(applied_measurement)

        with self._loop_lock:
            innovation_translation = (
                self._loop_pose_filter.last_innovation_translation_m
            )
            innovation_rotation = self._loop_pose_filter.last_innovation_rotation_deg
        if applied_measurement is not None:
            correction_translation = float(
                np.linalg.norm(applied_measurement[:3, 3])
            )
            correction_rotation = rotation_distance_deg(
                np.eye(4, dtype=np.float64), applied_measurement
            )
            self.get_logger().info(
                "accepted head-camera loop closure %d: correction=%.3f m / %.2f deg; "
                "pose_graph=%s"
                % (
                    loop_count,
                    correction_translation,
                    correction_rotation,
                    "optimized" if graph_optimized else "pending",
                )
            )
        payload = {
            **diagnostics,
            **{f"loop_{key}": value for key, value in localization.items()},
            "loop_localized": transition["localized"],
            "loop_confirmation_progress": transition["confirmation_progress"],
            "loop_confirmation_required": transition["confirmation_required"],
            "loop_accepted": measurement_changed,
            "loop_closures": loop_count,
            "loop_ekf_innovation_translation_m": round(innovation_translation, 4),
            "loop_ekf_innovation_rotation_deg": round(innovation_rotation, 3),
        }
        return payload, graph_optimized

    def _optimize_pose_graph(
        self, diagnostics: dict[str, object], *, trigger: str
    ) -> PoseGraphOptimizationResult:
        """Run one pending graph solve and expose bounded runtime diagnostics."""

        result = self._pose_graph.optimize()
        self._last_pose_graph_optimization_monotonic = time.monotonic()
        self._pose_graph_pending = False
        if result.optimized:
            self._pose_graph_optimization_count += 1
        diagnostics.update(
            {
                "pose_graph_trigger": trigger,
                "pose_graph_optimized": result.optimized,
                "pose_graph_success": result.success,
                "pose_graph_iterations": result.iterations,
                "pose_graph_initial_cost": round(result.initial_cost, 3),
                "pose_graph_final_cost": round(result.final_cost, 3),
                "pose_graph_optimization_ms": round(result.elapsed_ms, 1),
                "pose_graph_max_translation_correction_m": round(
                    result.max_translation_correction_m, 4
                ),
                "pose_graph_max_rotation_correction_deg": round(
                    result.max_rotation_correction_deg, 3
                ),
            }
        )
        return result

    def _rebuild_landmarks_from_observations(self):
        """Re-fuse retained stereo observations using optimized keyframe poses."""

        rebuilt = LandmarkMap(self._landmark_config)
        update = None
        for observation in self._mapping_observations:
            correction = self._pose_graph.correction_at(observation.stamp_ns)
            if correction is None:
                correction = self._map_to_odom(smoothed=False)
            map_to_left = compose_transform(correction, observation.odom_to_left)
            map_points = transform_points(map_to_left, observation.points_left)
            update = rebuilt.update(
                observation.source_keyframe_id,
                map_points,
                observation_id=observation.observation_id,
                descriptors=np.asarray(observation.descriptors, dtype=np.float32),
                scores=np.asarray(observation.scores, dtype=np.float32),
            )
        if update is None:
            raise RuntimeError("cannot rebuild an empty keyframe map")
        with self._map_lock:
            self._landmarks = rebuilt
            self._pointcloud_dirty = True
            self._feature_map_dirty = True
        return update

    def _is_keyframe(self, transform: np.ndarray) -> bool:
        previous = self._last_keyframe_transform
        if previous is None:
            return True
        translation = float(np.linalg.norm(transform[:3, 3] - previous[:3, 3]))
        rotation = rotation_distance_deg(previous, transform)
        return (
            translation >= self._args.keyframe_translation_m
            or rotation >= self._args.keyframe_rotation_deg
        )

    def _stationary_observation_due(self, stamp_ns: int) -> bool:
        """Collect a short temporal confirmation burst without graph nodes."""

        if self._keyframe_id <= 0:
            return False
        if (
            self._current_keyframe_observations
            >= self._args.stationary_bootstrap_max_observations
        ):
            return False
        with self._map_lock:
            confirmed = self._landmarks.confirmed_count()
        if confirmed >= self._args.stationary_bootstrap_min_points:
            return False
        interval_ns = int(
            self._args.stationary_observation_interval_sec * 1_000_000_000
        )
        return stamp_ns - self._last_map_observation_stamp_ns >= interval_ns

    def _publish_map(self) -> None:
        now = time.monotonic()
        with self._map_lock:
            pointcloud_due = (
                self._pointcloud_publisher is not None
                and (
                    self._pointcloud_dirty
                    or now - self._last_pointcloud_publish_monotonic
                    >= POINTCLOUD_REFRESH_INTERVAL_SEC
                )
                and (
                    now - self._last_pointcloud_publish_monotonic
                    >= POINTCLOUD_MIN_PUBLISH_INTERVAL_SEC
                )
            )
            points = self._landmarks.points() if pointcloud_due else None
            point_count = self._landmarks.confirmed_count()
            if pointcloud_due:
                self._pointcloud_dirty = False
            feature_due = (
                self._feature_map_dirty
                or now - self._last_feature_publish_monotonic >= 10.0
            ) and now - self._last_feature_publish_monotonic >= 1.0
            if feature_due:
                feature_points, descriptors = self._landmarks.descriptors()
                self._feature_map_dirty = False
            else:
                feature_points = descriptors = None
        header = Header()
        header.stamp = self.get_clock().now().to_msg()
        header.frame_id = self._map_frame
        if points is not None and self._pointcloud_publisher is not None:
            cloud = point_cloud2.create_cloud_xyz32(header, points.tolist())
            self._pointcloud_publisher.publish(cloud)
            self._last_pointcloud_publish_monotonic = now
        if feature_points is not None and descriptors is not None:
            self._feature_map_publisher.publish(
                descriptor_cloud(header, feature_points, descriptors)
            )
            self._last_feature_publish_monotonic = now
        status = String()
        status_payload = dict(self._latest_stats)
        status_payload["map_point_count"] = point_count
        status_payload["center_extrinsic_ready"] = self._imu_to_center is not None
        status_payload["pose_frame"] = self._camera_frame
        status.data = json.dumps(status_payload, separators=(",", ":"))
        self._status_publisher.publish(status)

    def _publish_path(self) -> None:
        if self._path_publisher is None:
            return
        with self._path_lock:
            raw_poses = list(self._path)
        if not raw_poses:
            return
        smooth_correction = self._map_to_odom()
        with self._graph_lock:
            graph_corrections = (
                [
                    self._pose_graph.correction_at(stamp_ns)
                    for stamp_ns, _odom_to_center in raw_poses
                ]
                if self._pose_graph.keyframe_count > 0
                else [None] * len(raw_poses)
            )
        poses = []
        for (stamp_ns, odom_to_center), graph_correction in zip(
            raw_poses, graph_corrections
        ):
            correction = (
                smooth_correction
                if graph_correction is None
                else graph_correction
            )
            poses.append(
                matrix_to_pose_stamped(
                    compose_transform(correction, odom_to_center),
                    stamp_ns,
                    self._map_frame,
                )
            )
        path = PathMsg()
        path.header.stamp = poses[-1].header.stamp
        path.header.frame_id = self._map_frame
        path.poses = poses
        self._path_publisher.publish(path)

    def _publish_latest_tf(self) -> None:
        with self._path_lock:
            latest_sample = self._path[-1] if self._path else None
        if latest_sample is None:
            return
        latest_stamp_ns, latest_odom_to_center = latest_sample
        smooth_correction = self._map_to_odom()
        latest = matrix_to_pose_stamped(
            compose_transform(smooth_correction, latest_odom_to_center),
            latest_stamp_ns,
            self._map_frame,
        )
        transform = TransformStamped()
        transform.header.frame_id = self._map_frame
        transform.header.stamp = self.get_clock().now().to_msg()
        transform.child_frame_id = self._camera_frame
        transform.transform.translation.x = latest.pose.position.x
        transform.transform.translation.y = latest.pose.position.y
        transform.transform.translation.z = latest.pose.position.z
        transform.transform.rotation = latest.pose.orientation
        self._tf_broadcaster.sendTransform(transform)


def configure_head_camera(args: argparse.Namespace) -> None:
    """Fill unspecified mapper inputs from the configured head camera."""

    config_path = Path(args.camera_config)
    try:
        with config_path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
        head = next(
            camera
            for camera in payload.get("cameras", [])
            if camera.get("enabled", True)
            and camera.get("teleop_role") == "head"
        )
    except (OSError, ValueError, StopIteration, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot resolve the head camera from {config_path}: {exc}") from exc

    namespace = str(head["namespace"])
    args.head_camera_name = str(head["name"])
    base = f"/{namespace}/camera"
    args.left_image_topic = args.left_image_topic or f"{base}/infra1/image_rect_raw"
    args.right_image_topic = args.right_image_topic or f"{base}/infra2/image_rect_raw"
    args.left_info_topic = args.left_info_topic or f"{base}/infra1/camera_info"
    args.right_info_topic = args.right_info_topic or f"{base}/infra2/camera_info"
    args.vio_topic = args.vio_topic or f"{base}/vio_100hz"
    args.imu_frame = args.imu_frame or f"{namespace}_camera_imu"
    args.left_frame = args.left_frame or f"{namespace}_camera_left"
    args.right_frame = args.right_frame or f"{namespace}_camera_right"


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--superglue-checkout",
        default=str(root / "data" / "models" / "SuperGluePretrainedNetwork"),
    )
    parser.add_argument(
        "--camera-config",
        default=str(root / "config" / "cameras.json"),
        help="resolve unspecified stereo/VIO inputs from the enabled head camera",
    )
    parser.add_argument("--backend", choices=("ipc", "official-torch"), default="ipc")
    parser.add_argument("--inference-socket", default="/run/superglue/matcher.sock")
    parser.add_argument("--superglue-weights", choices=("indoor", "outdoor"), default="indoor")
    parser.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    parser.add_argument("--left-image-topic")
    parser.add_argument("--right-image-topic")
    parser.add_argument("--left-info-topic")
    parser.add_argument("--right-info-topic")
    parser.add_argument("--vio-topic")
    parser.add_argument("--imu-frame")
    parser.add_argument("--left-frame")
    parser.add_argument("--right-frame")
    parser.add_argument("--map-frame", default="insight9_map")
    parser.add_argument("--mapping-camera-frame", default="insight9_mapping_camera_center")
    parser.add_argument("--mapping-hz", type=float, default=5.0)
    parser.add_argument("--pose-wait-ms", type=float, default=30.0)
    parser.add_argument("--stereo-tolerance-ms", type=float, default=20.0)
    parser.add_argument("--max-keypoints", type=int, default=1024)
    parser.add_argument("--keypoint-threshold", type=float, default=0.005)
    parser.add_argument("--match-threshold", type=float, default=0.2)
    parser.add_argument("--max-epipolar-error-px", type=float, default=1.5)
    parser.add_argument("--min-disparity-px", type=float, default=1.0)
    parser.add_argument("--min-depth-m", type=float, default=0.2)
    parser.add_argument("--max-depth-m", type=float, default=8.0)
    parser.add_argument("--max-reprojection-error-px", type=float, default=1.5)
    parser.add_argument("--keyframe-translation-m", type=float, default=0.05)
    parser.add_argument("--keyframe-rotation-deg", type=float, default=3.0)
    parser.add_argument("--voxel-size-m", type=float, default=0.04)
    parser.add_argument("--confirmation-observations", type=int, default=3)
    parser.add_argument(
        "--stationary-observation-interval-sec",
        type=float,
        default=0.4,
        help="interval between temporal confirmations that do not add graph keyframes",
    )
    parser.add_argument(
        "--stationary-bootstrap-min-points",
        type=int,
        default=80,
        help="stop stationary bootstrap once this many landmarks are confirmed",
    )
    parser.add_argument(
        "--stationary-bootstrap-max-observations",
        type=int,
        default=6,
        help="bound stationary bootstrap inference when a scene lacks texture",
    )
    parser.add_argument("--candidate-ttl-keyframes", type=int, default=12)
    parser.add_argument("--max-landmarks", type=int, default=100_000)
    parser.add_argument("--path-points", type=int, default=200)
    parser.add_argument("--path-interval-ms", type=int, default=50)
    parser.add_argument("--path-publish-hz", type=float, default=2.0)
    parser.add_argument(
        "--publish-debug-topics",
        action="store_true",
        help="publish RViz-only sparse points and historical Path topics",
    )
    parser.add_argument("--tf-publish-hz", type=float, default=5.0)
    parser.add_argument("--pose-publish-hz", type=float, default=50.0)
    parser.add_argument(
        "--loop-closure-enabled",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--loop-check-interval-keyframes", type=int, default=2)
    parser.add_argument("--loop-exclude-recent-keyframes", type=int, default=30)
    parser.add_argument("--loop-min-map-features", type=int, default=80)
    parser.add_argument("--loop-ratio-test", type=float, default=0.78)
    parser.add_argument("--loop-min-similarity", type=float, default=0.70)
    parser.add_argument("--loop-min-matches", type=int, default=20)
    parser.add_argument("--loop-min-inliers", type=int, default=15)
    parser.add_argument("--loop-min-inlier-ratio", type=float, default=0.55)
    parser.add_argument("--loop-max-reprojection-error-px", type=float, default=2.5)
    parser.add_argument("--loop-min-grid-cells", type=int, default=5)
    parser.add_argument("--loop-confirmation-frames", type=int, default=3)
    parser.add_argument("--loop-confirmation-translation-m", type=float, default=0.20)
    parser.add_argument("--loop-confirmation-rotation-deg", type=float, default=10.0)
    parser.add_argument("--loop-process-translation-std", type=float, default=0.02)
    parser.add_argument("--loop-process-rotation-std-deg", type=float, default=0.5)
    parser.add_argument("--loop-measurement-translation-std", type=float, default=0.08)
    parser.add_argument("--loop-measurement-rotation-std-deg", type=float, default=2.5)
    parser.add_argument("--loop-correction-time-constant-sec", type=float, default=0.75)
    parser.add_argument(
        "--pose-graph-enabled",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--pose-graph-odometry-translation-std", type=float, default=0.025
    )
    parser.add_argument(
        "--pose-graph-odometry-rotation-std-deg", type=float, default=0.75
    )
    parser.add_argument(
        "--pose-graph-loop-translation-std", type=float, default=0.05
    )
    parser.add_argument(
        "--pose-graph-loop-rotation-std-deg", type=float, default=2.0
    )
    parser.add_argument("--pose-graph-robust-delta", type=float, default=2.5)
    parser.add_argument("--pose-graph-max-iterations", type=int, default=10)
    parser.add_argument("--pose-graph-max-keyframes", type=int, default=600)
    parser.add_argument("--pose-graph-min-interval-sec", type=float, default=5.0)
    parser.add_argument("--pose-graph-force-translation-m", type=float, default=0.10)
    parser.add_argument("--pose-graph-force-rotation-deg", type=float, default=5.0)
    return parser


def main() -> int:
    args, ros_args = build_parser().parse_known_args()
    configure_head_camera(args)
    rclpy.init(args=ros_args)
    node: Optional[Insight9SparseMapper] = None
    executor: Optional[MultiThreadedExecutor] = None
    try:
        node = Insight9SparseMapper(args)
        executor = MultiThreadedExecutor(num_threads=2)
        executor.add_node(node)
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        if executor is not None:
            executor.shutdown()
        if node is not None:
            node.destroy_node()
        rclpy.try_shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
