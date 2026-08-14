#!/usr/bin/env python3

"""Localize configured hand cameras in the head-camera SuperPoint map."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from insight9_mapping_core import (  # noqa: E402
    AdaptiveRelocalizationConfig,
    AdaptiveRelocalizationPolicy,
    GlobalLocalizationConfig,
    IpcSuperGlueBackend,
    LocalizationConsensus,
    PoseBuffer,
    PoseSample,
    RelocalizationEkf,
    RelocalizationEkfConfig,
    VioContinuityConfig,
    VioContinuityStitcher,
    compose_transform,
    left_to_stereo_center,
    load_tcp_frame_calibrations,
    localize_features,
    matrix_from_pose,
    matrix_from_transform,
    normalize_descriptors,
    select_timestamp,
)
from insight3_localization_settings import (  # noqa: E402
    DEFAULT_GRIPPER_MASK_HEIGHT_RATIO,
    load_gripper_mask_height_ratio,
    validate_gripper_mask_height_ratio,
)

try:
    import rclpy
    from geometry_msgs.msg import PoseStamped, TransformStamped
    from nav_msgs.msg import Path as PathMsg
    from rclpy.duration import Duration
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data
    from rclpy.time import Time
    from sensor_msgs.msg import CameraInfo, Image, PointCloud2
    from std_msgs.msg import String
    from std_srvs.srv import Empty
    from tf2_ros import (
        Buffer,
        StaticTransformBroadcaster,
        TransformBroadcaster,
        TransformListener,
    )
except ImportError as exc:  # pragma: no cover - exercised in the ROS image
    raise SystemExit(f"ROS 2 Python dependencies are unavailable: {exc}") from exc



def localization_cameras(config_path: Path) -> dict[str, str]:
    """Return enabled hand camera names mapped to their ROS namespaces."""

    with Path(config_path).open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    cameras = {
        str(camera["name"]): str(camera["namespace"])
        for camera in payload.get("cameras", [])
        if camera.get("enabled", True)
        and camera.get("teleop_role") in {"left_hand", "right_hand"}
    }
    if not cameras:
        raise ValueError(f"no enabled hand cameras in {config_path}")
    return cameras


def stamp_to_ns(stamp) -> int:
    return int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)


def grayscale_image(message: Image) -> np.ndarray:
    """Return a contiguous luma image from mono8 or NV12 ROS payloads."""

    height, width, step = int(message.height), int(message.width), int(message.step)
    raw = np.frombuffer(message.data, dtype=np.uint8)
    encoding = message.encoding.lower()
    if step < width:
        raise ValueError(f"invalid {message.encoding} row stride")
    if encoding in {"mono8", "8uc1"}:
        required = height * step
        if raw.size < required:
            raise ValueError("invalid mono8 image buffer")
        return raw[:required].reshape(height, step)[:, :width].copy()
    if encoding == "nv12":
        total_rows, remainder = divmod(raw.size, step)
        if remainder or total_rows <= 0 or (total_rows * 2) % 3:
            raise ValueError("invalid NV12 image buffer")
        luma_height = total_rows * 2 // 3
        return raw[: luma_height * step].reshape(luma_height, step)[:, :width].copy()
    raise ValueError(f"unsupported global localization image: {message.encoding}")


def static_gripper_feature_keep_mask(
    keypoints: np.ndarray,
    image_shape: tuple[int, int],
    mask_height_ratio: float,
) -> np.ndarray:
    """Return features outside the static gripper strip at the image bottom."""

    points = np.asarray(keypoints)
    if points.ndim != 2 or points.shape[1] != 2:
        raise ValueError("keypoints must have shape (N, 2)")
    if len(image_shape) < 2 or int(image_shape[0]) <= 0:
        raise ValueError("image shape must contain a positive height")
    ratio = float(mask_height_ratio)
    if not 0.0 <= ratio < 1.0:
        raise ValueError("gripper mask height ratio must be in [0, 1)")
    cutoff_y = float(image_shape[0]) * (1.0 - ratio)
    return points[:, 1] < cutoff_y


def rotation_to_quaternion(rotation: np.ndarray) -> tuple[float, float, float, float]:
    matrix = np.asarray(rotation, dtype=np.float64).reshape(3, 3)
    trace = float(np.trace(matrix))
    if trace > 0.0:
        scale = 2.0 * np.sqrt(trace + 1.0)
        return (
            float((matrix[2, 1] - matrix[1, 2]) / scale),
            float((matrix[0, 2] - matrix[2, 0]) / scale),
            float((matrix[1, 0] - matrix[0, 1]) / scale),
            float(0.25 * scale),
        )
    diagonal = int(np.argmax(np.diag(matrix)))
    if diagonal == 0:
        scale = 2.0 * np.sqrt(
            1.0 + matrix[0, 0] - matrix[1, 1] - matrix[2, 2]
        )
        return (
            float(0.25 * scale),
            float((matrix[0, 1] + matrix[1, 0]) / scale),
            float((matrix[0, 2] + matrix[2, 0]) / scale),
            float((matrix[2, 1] - matrix[1, 2]) / scale),
        )
    if diagonal == 1:
        scale = 2.0 * np.sqrt(
            1.0 + matrix[1, 1] - matrix[0, 0] - matrix[2, 2]
        )
        return (
            float((matrix[0, 1] + matrix[1, 0]) / scale),
            float(0.25 * scale),
            float((matrix[1, 2] + matrix[2, 1]) / scale),
            float((matrix[0, 2] - matrix[2, 0]) / scale),
        )
    scale = 2.0 * np.sqrt(
        1.0 + matrix[2, 2] - matrix[0, 0] - matrix[1, 1]
    )
    return (
        float((matrix[0, 2] + matrix[2, 0]) / scale),
        float((matrix[1, 2] + matrix[2, 1]) / scale),
        float(0.25 * scale),
        float((matrix[1, 0] - matrix[0, 1]) / scale),
    )


def pose_message(transform: np.ndarray, stamp, frame_id: str) -> PoseStamped:
    result = PoseStamped()
    result.header.stamp = stamp
    result.header.frame_id = frame_id
    result.pose.position.x = float(transform[0, 3])
    result.pose.position.y = float(transform[1, 3])
    result.pose.position.z = float(transform[2, 3])
    x, y, z, w = rotation_to_quaternion(transform[:3, :3])
    result.pose.orientation.x = x
    result.pose.orientation.y = y
    result.pose.orientation.z = z
    result.pose.orientation.w = w
    return result


def parse_feature_cloud(message: PointCloud2) -> tuple[np.ndarray, np.ndarray]:
    """Decode the mapper's XYZ + float descriptor PointCloud2 payload."""

    fields = {field.name: field for field in message.fields}
    required = ("x", "y", "z", "descriptor")
    if any(name not in fields for name in required):
        raise ValueError("feature cloud is missing XYZ or descriptor fields")
    descriptor_field = fields["descriptor"]
    descriptor_dim = int(descriptor_field.count)
    if (
        fields["x"].offset != 0
        or fields["y"].offset != 4
        or fields["z"].offset != 8
        or descriptor_field.offset != 12
        or descriptor_dim <= 0
        or message.point_step != (3 + descriptor_dim) * 4
    ):
        raise ValueError("feature cloud layout is incompatible")
    count = int(message.width) * int(message.height)
    expected = count * int(message.point_step)
    if len(message.data) < expected:
        raise ValueError("feature cloud payload is truncated")
    dtype = ">f4" if message.is_bigendian else "<f4"
    packed = np.frombuffer(message.data, dtype=dtype, count=count * (3 + descriptor_dim))
    packed = packed.reshape(count, 3 + descriptor_dim).astype(np.float32, copy=True)
    return packed[:, :3], packed[:, 3:]


class CameraState:
    def __init__(
        self,
        name: str,
        config: GlobalLocalizationConfig,
        *,
        history_points: int,
        ekf_config: RelocalizationEkfConfig,
        adaptive_config: AdaptiveRelocalizationConfig,
        vio_continuity_config: VioContinuityConfig,
    ) -> None:
        self.name = name
        self.lock = threading.Lock()
        self.pose_buffer = PoseBuffer(max_bracket_gap_ns=50_000_000)
        self.history: deque[PoseSample] = deque(maxlen=history_points)
        self.latest_image: Optional[Image] = None
        self.camera_matrix: Optional[np.ndarray] = None
        self.imu_to_left: Optional[np.ndarray] = None
        self.imu_to_center: Optional[np.ndarray] = None
        self.consensus = LocalizationConsensus(config)
        self.pose_filter = RelocalizationEkf(ekf_config)
        self.relocalization_policy = AdaptiveRelocalizationPolicy(adaptive_config)
        self.vio_stitcher = VioContinuityStitcher(vio_continuity_config)
        self.vio_tracking_status = "UNKNOWN"
        self.vio_tracking_status_monotonic = 0.0
        self.last_relocalization_measurement: Optional[np.ndarray] = None
        self.hard_relocalizations = 0
        self.last_vio_stamp_ns = -1
        self.last_input_vio_stamp_ns = -1
        self.next_vio_buffer_stamp_ns = 0
        self.next_vio_publish_stamp_ns = 0
        self.last_image_stamp_ns = -1
        self.last_history_stamp_ns = -1
        self.next_process_monotonic = 0.0
        self.status: dict[str, object] = {"state": "waiting_for_inputs"}
        self.path = PathMsg()
        self.path.header.frame_id = "insight9_map"
        self.path_dirty = True
        self.transformed_history: deque[PoseStamped] = deque(maxlen=history_points)
        self.transformed_output_extrinsic: Optional[np.ndarray] = None
        self.last_transformed_stamp_ns = -1
        self.last_global_pose_publish_ns = -1


class Insight3GlobalLocalizer(Node):
    """Match configured hand-camera streams to the shared head-camera map."""

    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__("insight3_global_localizer")
        self._args = args
        self._map_frame = args.map_frame
        self._gripper_mask_height_ratio = validate_gripper_mask_height_ratio(
            args.gripper_mask_height_ratio
        )
        self._settings_config_path = Path(args.settings_config)
        self._settings_config_mtime_ns: Optional[int] = None
        self._refresh_gripper_mask_setting(force=True)
        config = GlobalLocalizationConfig(
            ratio_test=args.ratio_test,
            min_similarity=args.min_similarity,
            min_matches=args.min_matches,
            min_inliers=args.min_inliers,
            min_inlier_ratio=args.min_inlier_ratio,
            max_reprojection_error_px=args.max_reprojection_error_px,
            min_grid_cells=args.min_grid_cells,
            confirmation_frames=args.confirmation_frames,
            confirmation_translation_m=args.confirmation_translation_m,
            confirmation_rotation_deg=args.confirmation_rotation_deg,
        )
        ekf_config = RelocalizationEkfConfig(
            process_translation_std_m_sqrt_s=args.ekf_process_translation_std,
            process_rotation_std_deg_sqrt_s=args.ekf_process_rotation_std_deg,
            measurement_translation_std_m=args.ekf_measurement_translation_std,
            measurement_rotation_std_deg=args.ekf_measurement_rotation_std_deg,
            correction_time_constant_sec=args.ekf_correction_time_constant_sec,
        )
        adaptive_config = AdaptiveRelocalizationConfig(
            jump_translation_m=args.jump_translation_m,
            jump_rotation_deg=args.jump_rotation_deg,
        )
        vio_continuity_config = VioContinuityConfig(
            translation_threshold_m=args.vio_stitch_translation_m,
            rotation_threshold_deg=args.vio_stitch_rotation_deg,
            max_gap_ms=args.vio_stitch_max_gap_ms,
        )
        self._matcher = IpcSuperGlueBackend(Path(args.inference_socket))
        self._camera_namespaces = localization_cameras(Path(args.camera_config))
        camera_names = tuple(self._camera_namespaces)
        self._map_lock = threading.Lock()
        self._map_points = np.empty((0, 3), dtype=np.float32)
        self._normalized_map_descriptors = np.empty((0, 256), dtype=np.float32)
        self._cameras = {
            name: CameraState(
                name,
                config,
                history_points=args.path_points,
                ekf_config=ekf_config,
                adaptive_config=adaptive_config,
                vio_continuity_config=vio_continuity_config,
            )
            for name in camera_names
        }
        self._path_publishers = (
            {
                name: self.create_publisher(
                    PathMsg, f"insight_global/{name}/path", 1
                )
                for name in camera_names
            }
            if args.publish_debug_topics
            else {}
        )
        self._pose_publishers = {
            name: self.create_publisher(
                PoseStamped, f"insight_global/{name}/pose", 1
            )
            for name in camera_names
        }
        self._status_publishers = {
            name: self.create_publisher(
                String, f"insight_global/{name}/status", 1
            )
            for name in camera_names
        }
        self._reset_service = self.create_service(
            Empty, "insight_global/reset", self._on_reset
        )
        self._tcp_calibrations = load_tcp_frame_calibrations(
            Path(args.camera_config), camera_names
        )
        self._tf_broadcaster = TransformBroadcaster(self)
        self._static_tf_broadcaster = StaticTransformBroadcaster(self)
        self._tf_buffer = Buffer(cache_time=Duration(seconds=10.0))
        self._tf_listener = TransformListener(self._tf_buffer, self)
        self._publish_tcp_static_transforms()
        self.create_subscription(
            PointCloud2,
            args.feature_map_topic,
            self._on_feature_map,
            1,
        )
        self._camera_subscriptions = []
        for name, namespace in self._camera_namespaces.items():
            image_topic = args.image_topic_template.format(name=name)
            self._camera_subscriptions.extend(
                [
                    self.create_subscription(
                        Image,
                        image_topic,
                        self._image_callback(name),
                        qos_profile_sensor_data,
                    ),
                    self.create_subscription(
                        CameraInfo,
                        f"/{namespace}/camera/infra1/camera_info",
                        self._camera_info_callback(name),
                        qos_profile_sensor_data,
                    ),
                    self.create_subscription(
                        PoseStamped,
                        f"/{namespace}/camera/vio_100hz",
                        self._vio_callback(name),
                        qos_profile_sensor_data,
                    ),
                    self.create_subscription(
                        String,
                        f"/{namespace}/camera/vio_status",
                        self._vio_status_callback(name),
                        qos_profile_sensor_data,
                    ),
                ]
            )
            self.get_logger().info(f"{name} localization image <- {image_topic}")
        self._stop = threading.Event()
        self._worker = threading.Thread(
            target=self._worker_main, name="insight3-global-localization", daemon=True
        )
        self._worker.start()
        self.create_timer(0.5, self._resolve_extrinsics)
        if args.publish_debug_topics:
            self.create_timer(
                1.0 / max(args.path_publish_hz, 0.1), self._publish_paths
            )
        self.create_timer(
            1.0 / max(args.tf_publish_hz, 0.1), self._publish_tfs
        )
        self.create_timer(0.5, self._publish_status)
        self.get_logger().info(
            f"SuperPoint global localizer started for {', '.join(camera_names)}; "
            f"masking the bottom {self._gripper_mask_height_ratio:.1%} of both images"
        )
        if not args.publish_debug_topics:
            self.get_logger().info("debug Path topics disabled")

    def _publish_tcp_static_transforms(self) -> None:
        stamp = self.get_clock().now().to_msg()
        transforms = []
        for calibration in self._tcp_calibrations.values():
            transform = TransformStamped()
            transform.header.frame_id = calibration.parent_frame_id
            transform.header.stamp = stamp
            transform.child_frame_id = calibration.child_frame_id
            transform.transform.translation.x = calibration.translation_m[0]
            transform.transform.translation.y = calibration.translation_m[1]
            transform.transform.translation.z = calibration.translation_m[2]
            transform.transform.rotation.x = calibration.rotation_xyzw[0]
            transform.transform.rotation.y = calibration.rotation_xyzw[1]
            transform.transform.rotation.z = calibration.rotation_xyzw[2]
            transform.transform.rotation.w = calibration.rotation_xyzw[3]
            transforms.append(transform)
            self.get_logger().info(
                f"TCP frame: {calibration.parent_frame_id} -> "
                f"{calibration.child_frame_id}"
            )
        if transforms:
            self._static_tf_broadcaster.sendTransform(transforms)

    def _on_reset(self, _request: Empty.Request, response: Empty.Response) -> Empty.Response:
        with self._map_lock:
            self._map_points = np.empty((0, 3), dtype=np.float32)
            self._normalized_map_descriptors = np.empty(
                (0, 256), dtype=np.float32
            )
        for state in self._cameras.values():
            with state.lock:
                state.history.clear()
                state.pose_buffer.clear()
                state.vio_stitcher.reset()
                state.consensus = LocalizationConsensus(state.consensus.config)
                state.pose_filter.reset()
                state.last_relocalization_measurement = None
                state.hard_relocalizations = 0
                state.last_vio_stamp_ns = -1
                state.last_input_vio_stamp_ns = -1
                state.next_vio_buffer_stamp_ns = 0
                state.next_vio_publish_stamp_ns = 0
                state.last_image_stamp_ns = -1
                state.last_history_stamp_ns = -1
                state.transformed_history.clear()
                state.transformed_output_extrinsic = None
                state.last_transformed_stamp_ns = -1
                state.last_global_pose_publish_ns = -1
                state.path = PathMsg()
                state.path.header.frame_id = self._map_frame
                state.path_dirty = True
                state.status = {
                    "state": "waiting_for_map",
                    "localized": False,
                    "tracking_mode": "unlocalized",
                }
        self.get_logger().info("Cleared hand-camera global corrections for a new map")
        return response

    def destroy_node(self) -> bool:
        self._stop.set()
        self._worker.join(timeout=3.0)
        return super().destroy_node()

    def _on_feature_map(self, message: PointCloud2) -> None:
        try:
            points, descriptors = parse_feature_cloud(message)
        except ValueError as exc:
            self.get_logger().error(f"rejected feature map: {exc}")
            return
        if len(points) > self._args.max_map_features:
            indices = np.linspace(
                0, len(points) - 1, self._args.max_map_features, dtype=np.int64
            )
            points = points[indices]
            descriptors = descriptors[indices]
        # The parser returns views into one packed cloud. Detach the small XYZ
        # array so caching normalized descriptors does not retain the original
        # packed descriptor payload as a second full-size map.
        points = np.ascontiguousarray(points, dtype=np.float32)
        normalized_descriptors = normalize_descriptors(descriptors)
        with self._map_lock:
            self._map_points = points
            self._normalized_map_descriptors = normalized_descriptors

    def _image_callback(self, name: str):
        def callback(message: Image) -> None:
            state = self._cameras[name]
            with state.lock:
                state.latest_image = message

        return callback

    def _camera_info_callback(self, name: str):
        def callback(message: CameraInfo) -> None:
            projection = np.asarray(message.p, dtype=np.float64).reshape(3, 4)
            matrix = projection[:, :3]
            if matrix[0, 0] <= 0.0 or matrix[1, 1] <= 0.0:
                return
            state = self._cameras[name]
            with state.lock:
                state.camera_matrix = matrix

        return callback

    def _vio_status_callback(self, name: str):
        def callback(message: String) -> None:
            value = str(message.data).strip().upper() or "UNKNOWN"
            state = self._cameras[name]
            with state.lock:
                state.vio_tracking_status = value
                state.vio_tracking_status_monotonic = time.monotonic()

        return callback

    def _vio_callback(self, name: str):
        def callback(message: PoseStamped) -> None:
            stamp_ns = stamp_to_ns(message.header.stamp)
            state = self._cameras[name]
            with state.lock:
                input_reset = (
                    state.last_input_vio_stamp_ns >= 0
                    and stamp_ns < state.last_input_vio_stamp_ns
                )
                state.last_input_vio_stamp_ns = stamp_ns
                if input_reset:
                    state.next_vio_buffer_stamp_ns = 0
                    state.next_vio_publish_stamp_ns = 0
                buffer_due, state.next_vio_buffer_stamp_ns = select_timestamp(
                    stamp_ns,
                    state.next_vio_buffer_stamp_ns,
                    max(50.0, self._args.pose_publish_hz * 2.0),
                )
                if not buffer_due:
                    return
            pose = message.pose
            raw_sample = PoseSample(
                stamp_ns=stamp_ns,
                translation=np.array(
                    [pose.position.x, pose.position.y, pose.position.z],
                    dtype=np.float64,
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
                matrix_from_pose(raw_sample)
            except ValueError:
                return
            global_poses = []
            stitch_event = None
            with state.lock:
                previous_stitch_events = state.vio_stitcher.stitch_events
                status_age_sec = (
                    time.monotonic() - state.vio_tracking_status_monotonic
                )
                stitch_allowed = (
                    state.vio_tracking_status == "TRACKING_STATIC"
                    and status_age_sec <= 1.0
                )
                corrected_samples = state.vio_stitcher.push(
                    raw_sample, allow_stitch=stitch_allowed
                )
                if state.vio_stitcher.stitch_events > previous_stitch_events:
                    stitch_event = state.vio_stitcher.status()
                for sample in corrected_samples:
                    reset = state.pose_buffer.append(sample)
                    if reset:
                        state.history.clear()
                        state.consensus = LocalizationConsensus(
                            state.consensus.config
                        )
                        state.pose_filter.reset()
                        state.last_relocalization_measurement = None
                        state.hard_relocalizations = 0
                        state.last_vio_stamp_ns = -1
                        state.last_input_vio_stamp_ns = sample.stamp_ns
                        state.last_image_stamp_ns = -1
                        state.last_history_stamp_ns = -1
                        state.transformed_history.clear()
                        state.transformed_output_extrinsic = None
                        state.last_transformed_stamp_ns = -1
                        state.last_global_pose_publish_ns = -1
                        state.path = PathMsg()
                        state.path.header.frame_id = self._map_frame
                        state.path_dirty = True
                        state.status = {
                            "state": "waiting_for_relocalization",
                            "localized": False,
                            "tracking_mode": "unlocalized",
                        }
                    history_due = (
                        reset
                        or state.last_history_stamp_ns < 0
                        or sample.stamp_ns - state.last_history_stamp_ns
                        >= self._args.path_interval_ms * 1_000_000
                    )
                    if history_due:
                        state.history.append(sample)
                        state.last_history_stamp_ns = sample.stamp_ns
                        state.path_dirty = True
                    publish_due, state.next_vio_publish_stamp_ns = select_timestamp(
                        sample.stamp_ns,
                        state.next_vio_publish_stamp_ns,
                        self._args.pose_publish_hz,
                    )
                    if not publish_due:
                        continue
                    if state.last_vio_stamp_ns >= 0:
                        state.pose_filter.predict(
                            (sample.stamp_ns - state.last_vio_stamp_ns) / 1e9
                        )
                    state.last_vio_stamp_ns = sample.stamp_ns
                    correction = state.pose_filter.correction
                    extrinsic = (
                        None
                        if state.imu_to_center is None
                        else state.imu_to_center.copy()
                    )
                    state.last_global_pose_publish_ns = sample.stamp_ns
                    if correction is None or extrinsic is None:
                        continue
                    transform = correction @ matrix_from_pose(sample) @ extrinsic
                    sample_stamp = Time(nanoseconds=sample.stamp_ns).to_msg()
                    global_poses.append(
                        pose_message(transform, sample_stamp, self._map_frame)
                    )
            if stitch_event is not None:
                self.get_logger().warning(
                    "%s stitched VIO reset %d: %.3f m / %.2f deg"
                    % (
                        name,
                        stitch_event["events"],
                        stitch_event["last_event_translation_m"],
                        stitch_event["last_event_rotation_deg"],
                    )
                )
            for global_pose in global_poses:
                self._pose_publishers[name].publish(global_pose)

        return callback

    def _resolve_extrinsics(self) -> None:
        for name, state in self._cameras.items():
            namespace = self._camera_namespaces[name]
            with state.lock:
                imu_to_left = state.imu_to_left
                center_ready = state.imu_to_center is not None
            if imu_to_left is not None and center_ready:
                continue
            if imu_to_left is None:
                try:
                    transform = self._tf_buffer.lookup_transform(
                        f"{namespace}_camera_imu",
                        f"{namespace}_camera_left",
                        Time(),
                        timeout=Duration(seconds=0.05),
                    )
                except Exception:
                    continue
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
                with state.lock:
                    state.imu_to_left = imu_to_left
                self.get_logger().info(f"resolved {name} T_imu_left")
            try:
                transform = self._tf_buffer.lookup_transform(
                    f"{namespace}_camera_left",
                    f"{namespace}_camera_right",
                    Time(),
                    timeout=Duration(seconds=0.05),
                )
            except Exception:
                continue
            value = transform.transform
            left_to_right = matrix_from_transform(
                (value.translation.x, value.translation.y, value.translation.z),
                (value.rotation.x, value.rotation.y, value.rotation.z, value.rotation.w),
            )
            left_to_center = left_to_stereo_center(left_to_right)
            imu_to_center = compose_transform(imu_to_left, left_to_center)
            with state.lock:
                state.imu_to_center = imu_to_center
            baseline_m = float(np.linalg.norm(left_to_right[:3, 3]))
            self.get_logger().info(
                f"resolved {name} stereo center from {baseline_m:.4f} m baseline"
            )

    def _worker_main(self) -> None:
        period = 1.0 / max(self._args.localization_hz, 0.1)
        while not self._stop.wait(0.05):
            self._refresh_gripper_mask_setting()
            with self._map_lock:
                # Feature-map callbacks replace these arrays atomically. Local
                # references remain immutable for this localization pass.
                map_points = self._map_points
                map_descriptors = self._normalized_map_descriptors
            for name, state in self._cameras.items():
                now = time.monotonic()
                with state.lock:
                    if now < state.next_process_monotonic:
                        continue
                    message = state.latest_image
                    camera_matrix = (
                        None
                        if state.camera_matrix is None
                        else state.camera_matrix.copy()
                    )
                    imu_to_left = (
                        None if state.imu_to_left is None else state.imu_to_left.copy()
                    )
                    imu_to_center = (
                        None
                        if state.imu_to_center is None
                        else state.imu_to_center.copy()
                    )
                if (
                    message is None
                    or camera_matrix is None
                    or imu_to_left is None
                    or imu_to_center is None
                    or len(map_points) < self._args.min_map_features
                ):
                    with state.lock:
                        localized = state.pose_filter.initialized
                        state.status = {
                            "state": "vio_only" if localized else "waiting",
                            "localized": localized,
                            "tracking_mode": (
                                "vio_only" if localized else "unlocalized"
                            ),
                            "image_ready": message is not None,
                            "camera_info_ready": camera_matrix is not None,
                            "extrinsic_ready": (
                                imu_to_left is not None and imu_to_center is not None
                            ),
                            "left_extrinsic_ready": imu_to_left is not None,
                            "center_extrinsic_ready": imu_to_center is not None,
                            "map_features": len(map_points),
                            "need_map_features": self._args.min_map_features,
                        }
                        state.next_process_monotonic = now + 0.5
                    continue
                stamp_ns = stamp_to_ns(message.header.stamp)
                with state.lock:
                    if stamp_ns <= state.last_image_stamp_ns:
                        continue
                    pose = state.pose_buffer.lookup(stamp_ns)
                    if pose is None:
                        continue
                    state.last_image_stamp_ns = stamp_ns
                    state.next_process_monotonic = now + period
                    localization_config = state.consensus.config
                started = time.perf_counter()
                try:
                    image = grayscale_image(message)
                    features = self._matcher.extract(image)
                    feature_keep = static_gripper_feature_keep_mask(
                        features.keypoints,
                        image.shape,
                        self._gripper_mask_height_ratio,
                    )
                    query_keypoints = features.keypoints[feature_keep]
                    query_descriptors = features.descriptors[feature_keep]
                    masked_query_features = int(
                        len(features.keypoints) - len(query_keypoints)
                    )
                    odom_to_left = compose_transform(
                        matrix_from_pose(pose), imu_to_left
                    )
                    candidate, diagnostics = localize_features(
                        query_keypoints,
                        query_descriptors,
                        map_points,
                        map_descriptors,
                        camera_matrix,
                        odom_to_left,
                        image.shape,
                        localization_config,
                        map_descriptors_normalized=True,
                    )
                    diagnostics["raw_query_features"] = int(len(features.keypoints))
                    diagnostics["masked_query_features"] = masked_query_features
                    diagnostics["gripper_mask_height_ratio"] = (
                        self._gripper_mask_height_ratio
                    )
                    with state.lock:
                        transition = state.consensus.observe(candidate)
                        measurement = state.consensus.correction
                        measurement_changed = (
                            measurement is not None
                            and (
                                state.last_relocalization_measurement is None
                                or not np.array_equal(
                                    measurement,
                                    state.last_relocalization_measurement,
                                )
                            )
                        )
                        if measurement_changed:
                            correction_update = state.relocalization_policy.apply(
                                state.pose_filter, measurement
                            )
                            state.last_relocalization_measurement = measurement.copy()
                            state.path_dirty = True
                            transition["correction_mode"] = correction_update.mode
                            transition["correction_translation_m"] = round(
                                correction_update.translation_m, 4
                            )
                            transition["correction_rotation_deg"] = round(
                                correction_update.rotation_deg, 3
                            )
                            if correction_update.mode in {"initialize", "jump"}:
                                self._start_new_path_segment(state)
                            if correction_update.mode == "jump":
                                state.hard_relocalizations += 1
                                self.get_logger().warning(
                                    "%s hard relocalization %d: %.3f m / %.2f deg"
                                    % (
                                        name,
                                        state.hard_relocalizations,
                                        correction_update.translation_m,
                                        correction_update.rotation_deg,
                                    )
                                )
                        else:
                            transition["correction_mode"] = "none"
                        transition["hard_relocalizations"] = (
                            state.hard_relocalizations
                        )
                        localized = state.pose_filter.initialized
                        transition["localized"] = localized
                        if diagnostics["accepted"]:
                            tracking_mode = (
                                "map_matched" if localized else "verifying"
                            )
                        else:
                            tracking_mode = (
                                "vio_only" if localized else "unlocalized"
                            )
                        transition["tracking_mode"] = tracking_mode
                        transition["ekf_initialized"] = state.pose_filter.initialized
                        transition["ekf_innovation_translation_m"] = round(
                            state.pose_filter.last_innovation_translation_m, 4
                        )
                        transition["ekf_innovation_rotation_deg"] = round(
                            state.pose_filter.last_innovation_rotation_deg, 3
                        )
                        state.status = {
                            "state": (
                                "localized"
                                if tracking_mode == "map_matched"
                                else (
                                    "vio_only"
                                    if tracking_mode == "vio_only"
                                    else "localizing"
                                )
                            ),
                            "map_features": len(map_points),
                            "extract_ms": features.backend_inference_ms,
                            "total_ms": round(
                                (time.perf_counter() - started) * 1000.0, 1
                            ),
                            **diagnostics,
                            **transition,
                        }
                except Exception as exc:
                    with state.lock:
                        state.consensus.observe(None)
                        localized = state.pose_filter.initialized
                        state.status = {
                            "state": "error",
                            "error": str(exc),
                            "localized": localized,
                            "tracking_mode": (
                                "vio_only" if localized else "unlocalized"
                            ),
                        }
                    self.get_logger().error(f"{name} localization failed: {exc}")

    def _refresh_gripper_mask_setting(self, *, force: bool = False) -> None:
        try:
            mtime_ns = self._settings_config_path.stat().st_mtime_ns
        except OSError:
            mtime_ns = -1
        if not force and mtime_ns == self._settings_config_mtime_ns:
            return
        self._settings_config_mtime_ns = mtime_ns
        try:
            ratio = load_gripper_mask_height_ratio(
                self._settings_config_path,
                default=self._args.gripper_mask_height_ratio,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.get_logger().warning(
                f"keeping gripper mask ratio {self._gripper_mask_height_ratio}: {exc}"
            )
            return
        if ratio == self._gripper_mask_height_ratio:
            return
        previous = self._gripper_mask_height_ratio
        self._gripper_mask_height_ratio = ratio
        self.get_logger().info(
            f"updated gripper mask height ratio from {previous} to {ratio}"
        )

    def _start_new_path_segment(self, state: CameraState) -> None:
        state.transformed_history.clear()
        state.last_transformed_stamp_ns = -1
        state.last_global_pose_publish_ns = -1
        latest_history = tuple(state.history)[-1:]
        state.history.clear()
        state.history.extend(latest_history)
        state.path = PathMsg()
        state.path.header.frame_id = self._map_frame

    def _rebuild_path(self, state: CameraState) -> None:
        with state.lock:
            correction = state.pose_filter.correction
            extrinsic = (
                None if state.imu_to_center is None else state.imu_to_center.copy()
            )
            history = tuple(state.history)
            state.path_dirty = False
            if correction is None or extrinsic is None:
                return

            extrinsic_changed = (
                state.transformed_output_extrinsic is None
                or not np.allclose(
                    extrinsic,
                    state.transformed_output_extrinsic,
                    rtol=0.0,
                    atol=1e-12,
                )
            )
            if extrinsic_changed:
                state.transformed_history.clear()
                state.last_transformed_stamp_ns = -1
                state.last_global_pose_publish_ns = -1
                pending = history[-1:]
                state.history.clear()
                state.history.extend(pending)
            else:
                pending = tuple(
                    sample
                    for sample in history
                    if sample.stamp_ns > state.last_transformed_stamp_ns
                )

            for sample in pending:
                transform = correction @ matrix_from_pose(sample) @ extrinsic
                stamp = Time(nanoseconds=sample.stamp_ns).to_msg()
                state.transformed_history.append(
                    pose_message(transform, stamp, self._map_frame)
                )
                state.last_transformed_stamp_ns = sample.stamp_ns

            state.transformed_output_extrinsic = extrinsic
            path = PathMsg()
            path.header.frame_id = self._map_frame
            path.poses = list(state.transformed_history)
            if path.poses:
                path.header.stamp = path.poses[-1].header.stamp
            state.path = path

    def _publish_paths(self) -> None:
        if not self._path_publishers:
            return
        for name, state in self._cameras.items():
            with state.lock:
                path_dirty = state.path_dirty
            if path_dirty:
                self._rebuild_path(state)
            with state.lock:
                path = state.path
            if path.poses:
                self._path_publishers[name].publish(path)

    def _publish_tfs(self) -> None:
        now_stamp = self.get_clock().now().to_msg()
        for name, state in self._cameras.items():
            with state.lock:
                correction = state.pose_filter.correction
                extrinsic = (
                    None if state.imu_to_center is None else state.imu_to_center.copy()
                )
                latest_sample = state.history[-1] if state.history else None
            if correction is None or extrinsic is None or latest_sample is None:
                continue
            transform_matrix = (
                correction @ matrix_from_pose(latest_sample) @ extrinsic
            )
            latest = pose_message(
                transform_matrix,
                Time(nanoseconds=latest_sample.stamp_ns).to_msg(),
                self._map_frame,
            )
            transform = TransformStamped()
            transform.header.frame_id = self._map_frame
            transform.header.stamp = now_stamp
            transform.child_frame_id = f"{name}_global_camera_center"
            transform.transform.translation.x = latest.pose.position.x
            transform.transform.translation.y = latest.pose.position.y
            transform.transform.translation.z = latest.pose.position.z
            transform.transform.rotation = latest.pose.orientation
            self._tf_broadcaster.sendTransform(transform)

    def _publish_status(self) -> None:
        for name, state in self._cameras.items():
            with state.lock:
                status = dict(state.status)
                status["camera"] = name
                status["history_points"] = len(state.history)
                status["published_path_points"] = len(state.path.poses)
                status["ekf_initialized"] = state.pose_filter.initialized
                status["localized"] = state.pose_filter.initialized
                status["ekf_covariance_diagonal"] = (
                    state.pose_filter.covariance_diagonal
                )
                status["hard_relocalizations"] = state.hard_relocalizations
                status["jump_translation_m"] = (
                    state.relocalization_policy.config.jump_translation_m
                )
                status["jump_rotation_deg"] = (
                    state.relocalization_policy.config.jump_rotation_deg
                )
                vio_continuity = state.vio_stitcher.status()
                vio_status_age_sec = (
                    time.monotonic() - state.vio_tracking_status_monotonic
                )
                vio_continuity["device_tracking_status"] = (
                    state.vio_tracking_status
                )
                vio_continuity["auto_stitch_allowed"] = (
                    state.vio_tracking_status == "TRACKING_STATIC"
                    and vio_status_age_sec <= 1.0
                )
                status["vio_continuity"] = vio_continuity
            status["gripper_mask_height_ratio"] = self._gripper_mask_height_ratio
            status["pose_frame"] = f"{name}_global_camera_center"
            calibration = self._tcp_calibrations.get(name)
            status["tcp_frame"] = (
                calibration.child_frame_id if calibration is not None else None
            )
            status["tcp_calibrated"] = calibration is not None
            message = String()
            message.data = json.dumps(status, separators=(",", ":"))
            self._status_publishers[name].publish(message)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--camera-config",
        default=str(SCRIPT_DIR.parent / "config" / "cameras.json"),
    )
    parser.add_argument("--inference-socket", default="/run/superglue/matcher.sock")
    parser.add_argument(
        "--settings-config",
        default=str(SCRIPT_DIR.parent / "config" / "post_processing.json"),
    )
    parser.add_argument(
        "--feature-map-topic", default="/insight9_sparse_map/features"
    )
    parser.add_argument(
        "--image-topic-template",
        default="/insight_mapping/{name}/infra1/image_rect_raw",
    )
    parser.add_argument("--map-frame", default="insight9_map")
    parser.add_argument("--localization-hz", type=float, default=1.0)
    parser.add_argument(
        "--gripper-mask-height-ratio",
        type=float,
        default=DEFAULT_GRIPPER_MASK_HEIGHT_RATIO,
        help="mask this fraction of each Insight3 image from the bottom",
    )
    parser.add_argument("--max-map-features", type=int, default=20_000)
    parser.add_argument("--min-map-features", type=int, default=80)
    parser.add_argument("--ratio-test", type=float, default=0.80)
    parser.add_argument("--min-similarity", type=float, default=0.65)
    parser.add_argument("--min-matches", type=int, default=12)
    parser.add_argument("--min-inliers", type=int, default=10)
    parser.add_argument("--min-inlier-ratio", type=float, default=0.45)
    parser.add_argument("--max-reprojection-error-px", type=float, default=3.0)
    parser.add_argument("--min-grid-cells", type=int, default=4)
    parser.add_argument("--confirmation-frames", type=int, default=3)
    parser.add_argument("--confirmation-translation-m", type=float, default=0.20)
    parser.add_argument("--confirmation-rotation-deg", type=float, default=12.0)
    parser.add_argument("--path-points", type=int, default=200)
    parser.add_argument("--path-interval-ms", type=int, default=50)
    parser.add_argument("--path-publish-hz", type=float, default=2.0)
    parser.add_argument(
        "--publish-debug-topics",
        action="store_true",
        help="publish RViz-only historical Path topics",
    )
    parser.add_argument("--tf-publish-hz", type=float, default=5.0)
    parser.add_argument("--pose-publish-hz", type=float, default=50.0)
    parser.add_argument("--ekf-process-translation-std", type=float, default=0.02)
    parser.add_argument("--ekf-process-rotation-std-deg", type=float, default=0.5)
    parser.add_argument("--ekf-measurement-translation-std", type=float, default=0.10)
    parser.add_argument("--ekf-measurement-rotation-std-deg", type=float, default=3.0)
    parser.add_argument(
        "--ekf-correction-time-constant-sec", type=float, default=1.0
    )
    parser.add_argument("--jump-translation-m", type=float, default=0.15)
    parser.add_argument("--jump-rotation-deg", type=float, default=10.0)
    parser.add_argument("--vio-stitch-translation-m", type=float, default=0.03)
    parser.add_argument("--vio-stitch-rotation-deg", type=float, default=5.0)
    parser.add_argument("--vio-stitch-max-gap-ms", type=float, default=50.0)
    return parser


def main() -> int:
    args, ros_args = build_parser().parse_known_args()
    rclpy.init(args=ros_args)
    node: Optional[Insight3GlobalLocalizer] = None
    try:
        node = Insight3GlobalLocalizer(args)
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.destroy_node()
        rclpy.try_shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
