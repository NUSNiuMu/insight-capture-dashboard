#!/usr/bin/env python3

"""把两路 Insight3 定位到 Insight9 建立的同一稀疏地图。

每路相机保留独立的 VIO、PnP 共识、EKF 和轨迹状态，但共享 Insight9 描述子地图与
校准关键帧。首次定位优先执行“Insight3 图像到关键帧图像”的 SuperGlue 直接匹配；
失败或后续重定位再使用查询 SuperPoint 描述子到全局三维地图的匹配。
"""

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
from typing import Optional

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from insight_capture.runtime.mapping import (  # noqa: E402
    AdaptiveRelocalizationConfig,
    AdaptiveRelocalizationPolicy,
    CubeMarkerConfig,
    GlobalLocalizationConfig,
    IpcSuperGlueBackend,
    LocalizationConsensus,
    LocalizationCandidate,
    MultiCubeMarkerEstimator,
    PoseBuffer,
    PoseSample,
    RelocalizationEkf,
    RelocalizationEkfConfig,
    VioContinuityConfig,
    VioContinuityStitcher,
    associate_reference_points,
    compose_transform,
    grayscale_marker_image,
    left_to_stereo_center,
    load_tcp_frame_calibrations,
    load_cube_marker_config,
    localize_correspondences,
    localize_features,
    matrix_from_pose,
    matrix_from_transform,
    marker_map_to_odom,
    normalize_descriptors,
    rotation_distance_deg,
    select_timestamp,
)
from insight_capture.core.localization_settings import (  # noqa: E402
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
    from rclpy.qos import (
        DurabilityPolicy,
        QoSProfile,
        ReliabilityPolicy,
        qos_profile_sensor_data,
    )
    from rclpy.time import Time
    from sensor_msgs.msg import CameraInfo, CompressedImage, Image, PointCloud2
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


CAMERAS = ("insight3_a", "insight3_b")


def stamp_to_ns(stamp) -> int:
    return int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)


def grayscale_image(message: Image) -> np.ndarray:
    """从 mono8 或 NV12 ROS 消息提取连续亮度平面。"""

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
    """返回不在图像底部静态夹爪区域内的特征选择掩码。"""

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
    """解码 mapper 发布的 XYZ 与浮点描述子 PointCloud2。"""

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


def parse_calibration_keyframe_cloud(
    message: PointCloud2,
) -> tuple[np.ndarray, np.ndarray]:
    """解码校准关键帧中的地图 XYZ 与参考图像 UV 对应。"""

    fields = {field.name: field for field in message.fields}
    required = ("x", "y", "z", "u", "v")
    if any(name not in fields for name in required):
        raise ValueError("calibration keyframe cloud is missing XYZ or UV fields")
    if (
        tuple(fields[name].offset for name in required) != (0, 4, 8, 12, 16)
        or any(fields[name].count != 1 for name in required)
        or message.point_step != 20
    ):
        raise ValueError("calibration keyframe cloud layout is incompatible")
    count = int(message.width) * int(message.height)
    expected = count * int(message.point_step)
    if len(message.data) < expected:
        raise ValueError("calibration keyframe cloud payload is truncated")
    dtype = ">f4" if message.is_bigendian else "<f4"
    packed = np.frombuffer(message.data, dtype=dtype, count=count * 5)
    packed = packed.reshape(count, 5).astype(np.float32, copy=True)
    return packed[:, 3:5], packed[:, :3]


def static_gripper_image_mask(
    image_shape: tuple[int, int], mask_height_ratio: float
) -> np.ndarray:
    """生成底部静态夹爪区域的像素掩码，供直接 SuperGlue 匹配使用。"""

    height, width = int(image_shape[0]), int(image_shape[1])
    if height <= 0 or width <= 0:
        raise ValueError("image shape must be positive")
    ratio = float(mask_height_ratio)
    if not 0.0 <= ratio < 1.0:
        raise ValueError("gripper mask height ratio must be in [0, 1)")
    mask = np.zeros((height, width), dtype=bool)
    if ratio > 0.0:
        mask[int(height * (1.0 - ratio)) :, :] = True
    return mask


@dataclass(frozen=True)
class CalibrationReference:
    """时间戳一致的 Insight9 图像及其已三角化 UV/地图 XYZ。"""

    stamp_ns: int
    image: np.ndarray
    pixels: np.ndarray
    map_points: np.ndarray


class CameraState:
    """单路 Insight3 的同步、定位、滤波、连续性和发布状态。"""

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
        self.marker_consensus: Optional[LocalizationConsensus] = None
        self.pose_filter = RelocalizationEkf(ekf_config)
        self.relocalization_policy = AdaptiveRelocalizationPolicy(adaptive_config)
        self.vio_stitcher = VioContinuityStitcher(vio_continuity_config)
        self.vio_tracking_status = "UNKNOWN"
        self.vio_tracking_status_monotonic = 0.0
        self.last_relocalization_measurement: Optional[np.ndarray] = None
        self.last_marker_measurement: Optional[np.ndarray] = None
        self.marker_observations = 0
        self.marker_updates = 0
        self.marker_status: dict[str, object] = {"state": "disabled"}
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
    """把两路 Insight3 流匹配到共享的 Insight9 三维描述子地图。"""

    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__("insight3_global_localizer")
        self._args = args
        self._map_frame = args.map_frame
        if args.calibration_keyframe_association_px <= 0.0:
            raise ValueError(
                "calibration keyframe association distance must be positive"
            )
        self._gripper_mask_height_ratio = validate_gripper_mask_height_ratio(
            args.gripper_mask_height_ratio
        )
        self._settings_config_path = Path(args.settings_config)
        self._settings_config_mtime_ns: Optional[int] = None
        self._refresh_gripper_mask_setting(force=True)
        self._cube_marker_config = load_cube_marker_config(
            self._settings_config_path
        )
        unknown_marker_cameras = set(self._cube_marker_config.targets) - set(CAMERAS)
        if unknown_marker_cameras:
            raise ValueError(
                "cube marker targets reference unsupported cameras: "
                + ", ".join(sorted(unknown_marker_cameras))
            )
        # 两路相机使用相同几何门限，但各自维护独立的候选窗口和滤波状态。
        config = GlobalLocalizationConfig(
            ratio_test=args.ratio_test,
            min_similarity=args.min_similarity,
            min_matches=args.min_matches,
            min_inliers=args.min_inliers,
            min_inlier_ratio=args.min_inlier_ratio,
            max_reprojection_error_px=args.max_reprojection_error_px,
            min_grid_cells=args.min_grid_cells,
            confirmation_frames=args.confirmation_frames,
            confirmation_window=args.confirmation_window,
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
        # 与 Insight9 mapper 共享同一个串行 TensorRT worker，避免重复加载 GPU 引擎。
        self._matcher = IpcSuperGlueBackend(Path(args.inference_socket))
        self._map_lock = threading.Lock()
        self._map_points = np.empty((0, 3), dtype=np.float32)
        self._normalized_map_descriptors = np.empty((0, 256), dtype=np.float32)
        self._calibration_reference_lock = threading.Lock()
        # 图像和 UV/XYZ 通过两个 latched 话题到达，以时间戳为键等待配成完整参考。
        self._calibration_images: dict[int, np.ndarray] = {}
        self._calibration_points: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        self._calibration_reference: Optional[CalibrationReference] = None
        self._cameras = {
            name: CameraState(
                name,
                config,
                history_points=args.path_points,
                ekf_config=ekf_config,
                adaptive_config=adaptive_config,
                vio_continuity_config=vio_continuity_config,
            )
            for name in CAMERAS
        }
        marker_consensus_config = GlobalLocalizationConfig(
            min_matches=4,
            min_inliers=4,
            min_inlier_ratio=self._cube_marker_config.min_inlier_ratio,
            max_reprojection_error_px=(
                self._cube_marker_config.max_reprojection_error_px
            ),
            min_grid_cells=1,
            confirmation_frames=self._cube_marker_config.confirmation_frames,
            confirmation_window=self._cube_marker_config.confirmation_window,
            confirmation_translation_m=(
                self._cube_marker_config.confirmation_translation_m
            ),
            confirmation_rotation_deg=(
                self._cube_marker_config.confirmation_rotation_deg
            ),
        )
        for name in self._cube_marker_config.targets:
            state = self._cameras[name]
            state.marker_consensus = LocalizationConsensus(marker_consensus_config)
            state.marker_status = {"state": "waiting_for_inputs"}
        self._marker_estimator = (
            MultiCubeMarkerEstimator(self._cube_marker_config)
            if self._cube_marker_config.enabled
            else None
        )
        self._head_pose_buffer = PoseBuffer(max_bracket_gap_ns=50_000_000)
        self._head_marker_lock = threading.Lock()
        self._head_marker_camera_matrix: Optional[np.ndarray] = None
        self._head_center_to_rgb: Optional[np.ndarray] = None
        self._marker_work: queue.Queue[object] = queue.Queue(maxsize=1)
        self._marker_last_processed_stamp_ns = -1
        self._path_publishers = (
            {
                name: self.create_publisher(
                    PathMsg, f"insight_global/{name}/path", 1
                )
                for name in CAMERAS
            }
            if args.publish_debug_topics
            else {}
        )
        self._pose_publishers = {
            name: self.create_publisher(
                PoseStamped, f"insight_global/{name}/pose", 1
            )
            for name in CAMERAS
        }
        self._status_publishers = {
            name: self.create_publisher(
                String, f"insight_global/{name}/status", 1
            )
            for name in CAMERAS
        }
        self._reset_service = self.create_service(
            Empty, "insight_global/reset", self._on_reset
        )
        self._tcp_calibrations = load_tcp_frame_calibrations(
            Path(args.camera_config), CAMERAS
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
        calibration_qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.create_subscription(
            Image,
            args.calibration_keyframe_image_topic,
            self._on_calibration_keyframe_image,
            calibration_qos,
        )
        self.create_subscription(
            PointCloud2,
            args.calibration_keyframe_points_topic,
            self._on_calibration_keyframe_points,
            calibration_qos,
        )
        self._camera_subscriptions = []
        for name in CAMERAS:
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
                        f"/{name}/camera/infra1/camera_info",
                        self._camera_info_callback(name),
                        qos_profile_sensor_data,
                    ),
                    self.create_subscription(
                        PoseStamped,
                        f"/{name}/camera/vio_100hz",
                        self._vio_callback(name),
                        qos_profile_sensor_data,
                    ),
                    self.create_subscription(
                        String,
                        f"/{name}/camera/vio_status",
                        self._vio_status_callback(name),
                        qos_profile_sensor_data,
                    ),
                ]
            )
            self.get_logger().info(f"{name} localization image <- {image_topic}")
        if self._cube_marker_config.enabled:
            self.create_subscription(
                PoseStamped,
                self._cube_marker_config.head_pose_topic,
                self._head_pose_callback,
                qos_profile_sensor_data,
            )
            self.create_subscription(
                CameraInfo,
                self._cube_marker_config.camera_info_topic,
                self._head_marker_camera_info_callback,
                qos_profile_sensor_data,
            )
            marker_image_type = (
                CompressedImage
                if self._cube_marker_config.image_topic.endswith("/compressed")
                else Image
            )
            self.create_subscription(
                marker_image_type,
                self._cube_marker_config.image_topic,
                self._head_marker_image_callback,
                qos_profile_sensor_data,
            )
        self._stop = threading.Event()
        self._worker = threading.Thread(
            target=self._worker_main, name="insight3-global-localization", daemon=True
        )
        self._worker.start()
        self._marker_worker = (
            threading.Thread(
                target=self._marker_worker_main,
                name="cube-marker-relative-localization",
                daemon=True,
            )
            if self._cube_marker_config.enabled
            else None
        )
        if self._marker_worker is not None:
            self._marker_worker.start()
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
            "SuperPoint global localizer started for insight3_a and insight3_b; "
            f"masking the bottom {self._gripper_mask_height_ratio:.1%} of both images"
        )
        if not args.publish_debug_topics:
            self.get_logger().info("debug Path topics disabled")
        if self._cube_marker_config.enabled:
            self.get_logger().info(
                "cube marker relative localization enabled for "
                + ", ".join(sorted(self._cube_marker_config.targets))
            )

    def _publish_tcp_static_transforms(self) -> None:
        """只发布配置中真实存在的相机中心到 TCP 静态标定。"""

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
        """清除两路全局修正与历史轨迹，但不修改共享 Insight9 地图。"""

        with self._map_lock:
            self._map_points = np.empty((0, 3), dtype=np.float32)
            self._normalized_map_descriptors = np.empty(
                (0, 256), dtype=np.float32
            )
        with self._calibration_reference_lock:
            self._calibration_images.clear()
            self._calibration_points.clear()
            self._calibration_reference = None
        for state in self._cameras.values():
            with state.lock:
                state.history.clear()
                state.pose_buffer.clear()
                state.vio_stitcher.reset()
                state.consensus = LocalizationConsensus(state.consensus.config)
                state.pose_filter.reset()
                state.last_relocalization_measurement = None
                state.last_marker_measurement = None
                state.marker_observations = 0
                state.marker_updates = 0
                if state.marker_consensus is not None:
                    state.marker_consensus = LocalizationConsensus(
                        state.marker_consensus.config
                    )
                    state.marker_status = {"state": "waiting_for_inputs"}
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
        self._head_pose_buffer.clear()
        self._marker_last_processed_stamp_ns = -1
        while True:
            try:
                self._marker_work.get_nowait()
            except queue.Empty:
                break
        self.get_logger().info("Cleared Insight3 global corrections for a new map")
        return response

    def destroy_node(self) -> bool:
        self._stop.set()
        self._worker.join(timeout=3.0)
        if self._marker_worker is not None:
            self._marker_worker.join(timeout=3.0)
        return super().destroy_node()

    def _on_feature_map(self, message: PointCloud2) -> None:
        """原子替换共享三维描述子地图，并预归一化供重复查询使用。"""

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

    def _on_calibration_keyframe_image(self, message: Image) -> None:
        """缓存校准关键帧图像，等待同时间戳的 UV/XYZ 点云。"""

        stamp_ns = stamp_to_ns(message.header.stamp)
        try:
            image = grayscale_image(message)
        except ValueError as exc:
            self.get_logger().error(f"rejected calibration keyframe image: {exc}")
            return
        with self._calibration_reference_lock:
            self._calibration_images[stamp_ns] = image
            self._assemble_calibration_reference(stamp_ns)

    def _on_calibration_keyframe_points(self, message: PointCloud2) -> None:
        """缓存校准关键帧 UV/XYZ，等待同时间戳的图像。"""

        stamp_ns = stamp_to_ns(message.header.stamp)
        try:
            pixels, map_points = parse_calibration_keyframe_cloud(message)
        except ValueError as exc:
            self.get_logger().error(f"rejected calibration keyframe points: {exc}")
            return
        with self._calibration_reference_lock:
            if not len(map_points):
                self._calibration_images.clear()
                self._calibration_points.clear()
                self._calibration_reference = None
                return
            self._calibration_points[stamp_ns] = (pixels, map_points)
            self._assemble_calibration_reference(stamp_ns)

    def _assemble_calibration_reference(self, stamp_ns: int) -> None:
        """仅在图像和点云齐备时发布新的不可变校准参考。"""

        """Pair image and geometry callbacks while holding the reference lock."""

        image = self._calibration_images.get(stamp_ns)
        geometry = self._calibration_points.get(stamp_ns)
        if image is None or geometry is None:
            self._trim_calibration_parts()
            return
        pixels, map_points = geometry
        self._calibration_reference = CalibrationReference(
            stamp_ns=stamp_ns,
            image=image,
            pixels=pixels,
            map_points=map_points,
        )
        self._calibration_images.clear()
        self._calibration_points.clear()

    def _trim_calibration_parts(self) -> None:
        for cache in (self._calibration_images, self._calibration_points):
            while len(cache) > 3:
                del cache[min(cache)]

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

    def _head_pose_callback(self, message: PoseStamped) -> None:
        pose = message.pose
        sample = PoseSample(
            stamp_ns=stamp_to_ns(message.header.stamp),
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
            matrix_from_pose(sample)
        except ValueError:
            return
        reset = self._head_pose_buffer.append(sample)
        if reset:
            for state in self._cameras.values():
                with state.lock:
                    if state.marker_consensus is not None:
                        state.marker_consensus = LocalizationConsensus(
                            state.marker_consensus.config
                        )
                        state.last_marker_measurement = None
                        state.marker_status = {"state": "head_pose_reset"}

    def _head_marker_camera_info_callback(self, message: CameraInfo) -> None:
        projection = np.asarray(message.p, dtype=np.float64).reshape(3, 4)
        matrix = projection[:, :3]
        if (
            matrix[0, 0] <= 0.0
            or matrix[1, 1] <= 0.0
            or not np.all(np.isfinite(matrix))
        ):
            return
        with self._head_marker_lock:
            self._head_marker_camera_matrix = matrix

    def _head_marker_image_callback(self, message: object) -> None:
        try:
            self._marker_work.put_nowait(message)
        except queue.Full:
            try:
                self._marker_work.get_nowait()
            except queue.Empty:
                pass
            try:
                self._marker_work.put_nowait(message)
            except queue.Full:
                pass

    def _set_marker_rejection(
        self, name: str, rejection: str, stamp_ns: int
    ) -> None:
        state = self._cameras[name]
        with state.lock:
            progress = {}
            if state.marker_consensus is not None:
                progress = state.marker_consensus.observe(None)
            state.marker_status = {
                "state": "waiting",
                "rejection": rejection,
                "image_stamp_ns": int(stamp_ns),
                **progress,
            }

    def _marker_worker_main(self) -> None:
        estimator = self._marker_estimator
        if estimator is None:
            return
        period_sec = 1.0 / self._cube_marker_config.detection_hz
        next_process_monotonic = 0.0
        while not self._stop.is_set():
            try:
                message = self._marker_work.get(timeout=0.2)
            except queue.Empty:
                continue
            delay_sec = next_process_monotonic - time.monotonic()
            if delay_sec > 0.0 and self._stop.wait(delay_sec):
                break
            while True:
                try:
                    message = self._marker_work.get_nowait()
                except queue.Empty:
                    break
            next_process_monotonic = time.monotonic() + period_sec
            stamp_ns = stamp_to_ns(message.header.stamp)
            if stamp_ns <= self._marker_last_processed_stamp_ns:
                continue
            self._marker_last_processed_stamp_ns = stamp_ns
            with self._head_marker_lock:
                camera_matrix = (
                    None
                    if self._head_marker_camera_matrix is None
                    else self._head_marker_camera_matrix.copy()
                )
                head_center_to_rgb = (
                    None
                    if self._head_center_to_rgb is None
                    else self._head_center_to_rgb.copy()
                )
            if camera_matrix is None:
                for name in self._cube_marker_config.targets:
                    self._set_marker_rejection(name, "missing_camera_info", stamp_ns)
                continue
            if head_center_to_rgb is None:
                for name in self._cube_marker_config.targets:
                    self._set_marker_rejection(name, "missing_head_rgb_extrinsic", stamp_ns)
                continue
            deadline = (
                time.monotonic() + self._cube_marker_config.pose_wait_ms / 1000.0
            )
            head_pose = self._head_pose_buffer.lookup(stamp_ns)
            while head_pose is None and time.monotonic() < deadline:
                if self._stop.wait(0.005):
                    return
                head_pose = self._head_pose_buffer.lookup(stamp_ns)
            if head_pose is None:
                for name in self._cube_marker_config.targets:
                    self._set_marker_rejection(name, "missing_head_pose_bracket", stamp_ns)
                continue
            try:
                gray = grayscale_marker_image(message)
                estimates = estimator.detect(gray, camera_matrix)
            except Exception as exc:
                for name in self._cube_marker_config.targets:
                    self._set_marker_rejection(
                        name, f"detection_error:{exc}", stamp_ns
                    )
                continue
            map_from_head_center = matrix_from_pose(head_pose)
            for name, target in self._cube_marker_config.targets.items():
                estimate = estimates.get(name)
                if estimate is None:
                    self._set_marker_rejection(name, "marker_not_detected", stamp_ns)
                    continue
                state = self._cameras[name]
                with state.lock:
                    vio_sample = state.pose_buffer.lookup(stamp_ns)
                    imu_to_center = (
                        None
                        if state.imu_to_center is None
                        else state.imu_to_center.copy()
                    )
                    consensus = state.marker_consensus
                    if vio_sample is None or imu_to_center is None or consensus is None:
                        rejection = (
                            "missing_gripper_vio_bracket"
                            if vio_sample is None
                            else "missing_gripper_extrinsic"
                        )
                        progress = (
                            consensus.observe(None) if consensus is not None else {}
                        )
                        state.marker_status = {
                            "state": "waiting",
                            "rejection": rejection,
                            "image_stamp_ns": int(stamp_ns),
                            **progress,
                        }
                        continue
                    odom_from_camera_center = (
                        matrix_from_pose(vio_sample) @ imu_to_center
                    )
                    map_from_camera, map_from_odom = marker_map_to_odom(
                        map_from_head_center,
                        head_center_to_rgb,
                        estimate.rgb_from_cube,
                        target.cube_from_camera_center,
                        odom_from_camera_center,
                    )
                    candidate = LocalizationCandidate(
                        map_to_camera=map_from_camera,
                        map_to_odom=map_from_odom,
                        matches=estimate.corners,
                        inliers=estimate.inliers,
                        inlier_ratio=estimate.inlier_ratio,
                        median_reprojection_error_px=(
                            estimate.median_reprojection_error_px
                        ),
                        grid_cells=len(estimate.marker_ids),
                    )
                    transition = consensus.observe(candidate)
                    measurement = consensus.correction
                    measurement_changed = (
                        measurement is not None
                        and (
                            state.last_marker_measurement is None
                            or not np.array_equal(
                                measurement, state.last_marker_measurement
                            )
                        )
                    )
                    correction_mode = "none"
                    if measurement_changed:
                        state.last_marker_measurement = measurement.copy()
                        state.marker_observations += 1
                        if self._cube_marker_config.apply_corrections:
                            correction_update = state.relocalization_policy.apply(
                                state.pose_filter,
                                measurement,
                                translation_std_m=(
                                    self._cube_marker_config.measurement_translation_std_m
                                ),
                                rotation_std_deg=(
                                    self._cube_marker_config.measurement_rotation_std_deg
                                ),
                            )
                            state.marker_updates += 1
                            state.path_dirty = True
                            correction_mode = correction_update.mode
                            transition["correction_translation_m"] = round(
                                correction_update.translation_m, 4
                            )
                            transition["correction_rotation_deg"] = round(
                                correction_update.rotation_deg, 3
                            )
                            if correction_mode in {"initialize", "jump"}:
                                self._start_new_path_segment(state)
                            if correction_mode == "jump":
                                state.hard_relocalizations += 1
                                self.get_logger().warning(
                                    "%s cube marker hard relocalization %d: %.3f m / %.2f deg"
                                    % (
                                        name,
                                        state.hard_relocalizations,
                                        correction_update.translation_m,
                                        correction_update.rotation_deg,
                                    )
                                )
                        else:
                            correction_mode = "shadow"
                            current = state.pose_filter.correction
                            if current is not None:
                                transition["shadow_translation_delta_m"] = round(
                                    float(
                                        np.linalg.norm(
                                            measurement[:3, 3] - current[:3, 3]
                                        )
                                    ),
                                    4,
                                )
                                transition["shadow_rotation_delta_deg"] = round(
                                    rotation_distance_deg(current, measurement), 3
                                )
                    transition["correction_mode"] = correction_mode
                    state.marker_status = {
                        "state": "matched",
                        "rejection": None,
                        "image_stamp_ns": int(stamp_ns),
                        "marker_ids": list(estimate.marker_ids),
                        "corners": estimate.corners,
                        "inliers": estimate.inliers,
                        "inlier_ratio": round(estimate.inlier_ratio, 4),
                        "median_reprojection_error_px": round(
                            estimate.median_reprojection_error_px, 3
                        ),
                        "max_reprojection_error_px": round(
                            estimate.max_reprojection_error_px, 3
                        ),
                        "apply_corrections": (
                            self._cube_marker_config.apply_corrections
                        ),
                        "observations": state.marker_observations,
                        "updates": state.marker_updates,
                        **transition,
                    }
                    if (
                        measurement_changed
                        and self._cube_marker_config.apply_corrections
                    ):
                        state.status = {
                            **state.status,
                            "state": "localized",
                            "localized": state.pose_filter.initialized,
                            "tracking_mode": "marker_matched",
                        }

    def _vio_status_callback(self, name: str):
        def callback(message: String) -> None:
            value = str(message.data).strip().upper() or "UNKNOWN"
            state = self._cameras[name]
            with state.lock:
                state.vio_tracking_status = value
                state.vio_tracking_status_monotonic = time.monotonic()

        return callback

    def _vio_callback(self, name: str):
        """生成单路 VIO 回调：先做连续性拼接，再外推并发布全局位姿。"""

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
                        state.last_marker_measurement = None
                        if state.marker_consensus is not None:
                            state.marker_consensus = LocalizationConsensus(
                                state.marker_consensus.config
                            )
                            state.marker_status = {
                                "state": "waiting_for_vio_relocalization"
                            }
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
        """分别解析两路 IMU→左目→双目中心外参。"""

        for name, state in self._cameras.items():
            with state.lock:
                imu_to_left = state.imu_to_left
                center_ready = state.imu_to_center is not None
            if imu_to_left is not None and center_ready:
                continue
            if imu_to_left is None:
                try:
                    transform = self._tf_buffer.lookup_transform(
                        f"{name}_camera_imu",
                        f"{name}_camera_left",
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
                    f"{name}_camera_left",
                    f"{name}_camera_right",
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
        if not self._cube_marker_config.enabled:
            return
        with self._head_marker_lock:
            if self._head_center_to_rgb is not None:
                return
        try:
            left_to_right_msg = self._tf_buffer.lookup_transform(
                self._cube_marker_config.head_left_frame,
                self._cube_marker_config.head_right_frame,
                Time(),
                timeout=Duration(seconds=0.05),
            )
            left_to_rgb_msg = self._tf_buffer.lookup_transform(
                self._cube_marker_config.head_left_frame,
                self._cube_marker_config.head_rgb_frame,
                Time(),
                timeout=Duration(seconds=0.05),
            )
        except Exception:
            return
        right_value = left_to_right_msg.transform
        left_to_right = matrix_from_transform(
            (
                right_value.translation.x,
                right_value.translation.y,
                right_value.translation.z,
            ),
            (
                right_value.rotation.x,
                right_value.rotation.y,
                right_value.rotation.z,
                right_value.rotation.w,
            ),
        )
        rgb_value = left_to_rgb_msg.transform
        left_to_rgb = matrix_from_transform(
            (
                rgb_value.translation.x,
                rgb_value.translation.y,
                rgb_value.translation.z,
            ),
            (
                rgb_value.rotation.x,
                rgb_value.rotation.y,
                rgb_value.rotation.z,
                rgb_value.rotation.w,
            ),
        )
        left_to_center = left_to_stereo_center(left_to_right)
        head_center_to_rgb = np.linalg.inv(left_to_center) @ left_to_rgb
        with self._head_marker_lock:
            self._head_center_to_rgb = head_center_to_rgb
        self.get_logger().info(
            "resolved Insight9 stereo-center to RGB extrinsic for cube markers"
        )

    def _worker_main(self) -> None:
        """低频轮询两路最新图像，执行直接关键帧或描述子地图定位。

        worker 不处理高频 VIO；它只更新低频 ``T_map_odom`` 观测。VIO 回调随后把该
        修正与每个局部位姿组合，形成连续高频全局输出。
        """

        period = 1.0 / max(self._args.localization_hz, 0.1)
        while not self._stop.wait(0.05):
            self._refresh_gripper_mask_setting()
            with self._map_lock:
                # Feature-map callbacks replace these arrays atomically. Local
                # references remain immutable for this localization pass.
                map_points = self._map_points
                map_descriptors = self._normalized_map_descriptors
            with self._calibration_reference_lock:
                calibration_reference = self._calibration_reference
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
                    localized_before = state.pose_filter.initialized
                # 首次定位可由单个高质量校准关键帧启动，不必等待三帧确认地图成熟。
                descriptor_map_ready = (
                    len(map_points) >= self._args.min_map_features
                )
                calibration_reference_ready = (
                    calibration_reference is not None
                    and len(calibration_reference.map_points)
                    >= self._args.min_matches
                )
                if (
                    message is None
                    or camera_matrix is None
                    or imu_to_left is None
                    or imu_to_center is None
                    or not (
                        descriptor_map_ready or calibration_reference_ready
                    )
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
                            "calibration_keyframe_ready": (
                                calibration_reference_ready
                            ),
                            "calibration_keyframe_points": (
                                len(calibration_reference.map_points)
                                if calibration_reference is not None
                                else 0
                            ),
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
                    # PnP 使用左目成像模型；最终发布时再转换到双目中心设备坐标。
                    odom_to_left = compose_transform(
                        matrix_from_pose(pose), imu_to_left
                    )
                    candidate = None
                    diagnostics: dict[str, object] = {}
                    inference_ms: Optional[float] = None
                    direct_summary: dict[str, object] = {}
                    # 尚未全局初始化时优先做图像到图像 SuperGlue：它保留关键点上下文，
                    # 通常比把查询描述子直接扫全局地图更适合跨设备首次校准。
                    if (
                        not localized_before
                        and calibration_reference_ready
                        and calibration_reference is not None
                    ):
                        if image.shape == calibration_reference.image.shape:
                            direct_matches = self._matcher.match(
                                image,
                                calibration_reference.image,
                                left_mask=static_gripper_image_mask(
                                    image.shape,
                                    self._gripper_mask_height_ratio,
                                ),
                            )
                            # 参考端匹配坐标关联到同帧已三角化 XYZ，形成 2D-3D PnP 输入。
                            direct_image_points, direct_object_points = (
                                associate_reference_points(
                                    direct_matches.left_points,
                                    direct_matches.right_points,
                                    calibration_reference.pixels,
                                    calibration_reference.map_points,
                                    max_distance_px=(
                                        self._args.calibration_keyframe_association_px
                                    ),
                                )
                            )
                            inference_ms = direct_matches.backend_inference_ms
                            diagnostics = {
                                "query_features": direct_matches.detected_left,
                                "map_features": len(
                                    calibration_reference.map_points
                                ),
                                "descriptor_matches": len(direct_image_points),
                                "descriptor_match_ms": (
                                    direct_matches.backend_inference_ms
                                ),
                                "median_similarity": (
                                    round(float(np.median(direct_matches.scores)), 4)
                                    if len(direct_matches.scores)
                                    else None
                                ),
                                "inliers": 0,
                                "inlier_ratio": 0.0,
                                "median_reprojection_error_px": None,
                                "grid_cells": 0,
                                "accepted": False,
                                "rejection": None,
                                "localization_method": "superglue_keyframe",
                                "direct_superglue_matches": len(
                                    direct_matches.left_points
                                ),
                                "direct_3d_matches": len(direct_image_points),
                                "calibration_keyframe_stamp_ns": (
                                    calibration_reference.stamp_ns
                                ),
                            }
                            candidate, diagnostics = localize_correspondences(
                                direct_image_points,
                                direct_object_points,
                                camera_matrix,
                                odom_to_left,
                                image.shape,
                                localization_config,
                                diagnostics=diagnostics,
                            )
                            direct_summary = {
                                "direct_accepted": diagnostics["accepted"],
                                "direct_rejection": diagnostics["rejection"],
                                "direct_superglue_matches": diagnostics[
                                    "direct_superglue_matches"
                                ],
                                "direct_3d_matches": diagnostics[
                                    "direct_3d_matches"
                                ],
                                "direct_inliers": diagnostics["inliers"],
                                "direct_inlier_ratio": diagnostics[
                                    "inlier_ratio"
                                ],
                                "direct_grid_cells": diagnostics["grid_cells"],
                            }
                        else:
                            direct_summary = {
                                "direct_accepted": False,
                                "direct_rejection": "image_shape_mismatch",
                            }
                    # 直接匹配不可用或未通过几何门限时，回退到全局描述子地图。
                    if candidate is None and descriptor_map_ready:
                        features = self._matcher.extract(image)
                        inference_ms = features.backend_inference_ms
                        # 固定夹爪属于相机自身而非环境，参与地图匹配会制造稳定假内点。
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
                        diagnostics["raw_query_features"] = int(
                            len(features.keypoints)
                        )
                        diagnostics["masked_query_features"] = (
                            masked_query_features
                        )
                        diagnostics["localization_method"] = "descriptor_map"
                        diagnostics.update(direct_summary)
                    elif not diagnostics:
                        diagnostics = {
                            "accepted": False,
                            "rejection": (
                                "descriptor_map_not_ready"
                                if localized_before
                                and calibration_reference_ready
                                else "calibration_keyframe_unavailable"
                            ),
                            "localization_method": (
                                "vio_only"
                                if localized_before
                                else "none"
                            ),
                        }
                    diagnostics["calibration_keyframe_ready"] = (
                        calibration_reference_ready
                    )
                    diagnostics["calibration_keyframe_points"] = (
                        len(calibration_reference.map_points)
                        if calibration_reference is not None
                        else 0
                    )
                    diagnostics["gripper_mask_height_ratio"] = (
                        self._gripper_mask_height_ratio
                    )
                    with state.lock:
                        # 单帧 PnP 只进入候选窗口；达到多帧一致性后才更新全局修正。
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
                            # 小漂移由 EKF 平滑吸收，大漂移立即跳转并切分显示轨迹。
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
                        # map_matched 表示本次确有地图观测；vio_only 仅表示沿用旧修正外推。
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
                            "extract_ms": inference_ms,
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
                status["cube_marker"] = dict(state.marker_status)
            status["gripper_mask_height_ratio"] = self._gripper_mask_height_ratio
            status["cube_marker_enabled"] = (
                self._cube_marker_config.enabled
                and name in self._cube_marker_config.targets
            )
            status["cube_marker_apply_corrections"] = (
                self._cube_marker_config.enabled
                and self._cube_marker_config.apply_corrections
                and name in self._cube_marker_config.targets
            )
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
        default=str(PROJECT_ROOT / "config" / "cameras.json"),
    )
    parser.add_argument("--inference-socket", default="/run/superglue/matcher.sock")
    parser.add_argument(
        "--settings-config",
        default=str(
            PROJECT_ROOT / "config" / "runtime.json"
            if (PROJECT_ROOT / "config" / "runtime.json").is_file()
            else PROJECT_ROOT / "config" / "post_processing.json"
        ),
    )
    parser.add_argument(
        "--feature-map-topic", default="/insight9_sparse_map/features"
    )
    parser.add_argument(
        "--calibration-keyframe-image-topic",
        default="/insight9_sparse_map/calibration_keyframe/image",
    )
    parser.add_argument(
        "--calibration-keyframe-points-topic",
        default="/insight9_sparse_map/calibration_keyframe/points",
    )
    parser.add_argument(
        "--calibration-keyframe-association-px", type=float, default=0.75
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
    parser.add_argument("--confirmation-window", type=int, default=5)
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
