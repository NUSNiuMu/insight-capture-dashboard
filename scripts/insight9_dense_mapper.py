#!/usr/bin/env python3

"""Build a dense Insight9 stereo point cloud and voxel-fused world map."""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from insight9_mapping_core import (  # noqa: E402
    DenseStereoConfig,
    DenseStereoEstimator,
    DenseVoxelMap,
    PoseBuffer,
    PoseSample,
    StereoCalibration,
    StereoPair,
    StereoPairSynchronizer,
    compose_transform,
    matrix_from_pose,
    matrix_from_transform,
    rotation_distance_deg,
    transform_points,
)

try:
    import rclpy
    from geometry_msgs.msg import PoseStamped
    from rclpy.duration import Duration
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data
    from rclpy.time import Time
    from sensor_msgs.msg import CameraInfo, Image, PointCloud2
    from sensor_msgs_py import point_cloud2
    from std_msgs.msg import Header, String
    from tf2_ros import Buffer, TransformListener
except ImportError as exc:  # pragma: no cover - exercised in the ROS image
    raise SystemExit(f"ROS 2 Python dependencies are unavailable: {exc}") from exc


def stamp_to_ns(stamp) -> int:
    return int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)


def image_to_mono8(message: Image) -> np.ndarray:
    """Decode a row-padded mono8 ROS image."""

    height, width, step = int(message.height), int(message.width), int(message.step)
    if message.encoding.lower() not in {"mono8", "8uc1"}:
        raise ValueError(f"dense stereo requires mono8 input, got {message.encoding}")
    raw = np.frombuffer(message.data, dtype=np.uint8)
    required = height * step
    if step < width or raw.size < required:
        raise ValueError("invalid mono8 image buffer")
    return raw[:required].reshape(height, step)[:, :width].copy()


class Insight9DenseMapper(Node):
    """Compute dense stereo off the ROS callback thread and fuse keyframes."""

    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__("insight9_dense_mapper")
        self._args = args
        self._map_frame = args.map_frame
        self._pose_buffer = PoseBuffer(max_bracket_gap_ns=50_000_000)
        self._stereo_sync: StereoPairSynchronizer[Image] = StereoPairSynchronizer(
            tolerance_ns=int(args.stereo_tolerance_ms * 1_000_000)
        )
        self._estimator = DenseStereoEstimator(
            DenseStereoConfig(
                min_disparity_px=args.min_disparity_px,
                num_disparities=args.num_disparities,
                block_size=args.block_size,
                min_depth_m=args.min_depth_m,
                max_depth_m=args.max_depth_m,
                pixel_stride=args.pixel_stride,
                max_points=args.max_current_points,
            )
        )
        self._voxel_map = DenseVoxelMap(
            voxel_size_m=args.voxel_size_m,
            max_voxels=args.max_voxels,
        )
        self._calibration_lock = threading.Lock()
        self._left_info: Optional[CameraInfo] = None
        self._right_info: Optional[CameraInfo] = None
        self._calibration: Optional[StereoCalibration] = None
        self._imu_to_left: Optional[np.ndarray] = None
        self._tf_buffer = Buffer(cache_time=Duration(seconds=10.0))
        self._tf_listener = TransformListener(self._tf_buffer, self)
        self._work: queue.Queue[StereoPair[Image]] = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._worker = threading.Thread(
            target=self._worker_main, name="dense-stereo", daemon=True
        )
        self._data_lock = threading.Lock()
        self._current_points = np.empty((0, 3), dtype=np.float32)
        self._latest_stats = {"state": "waiting_for_inputs"}
        self._last_processed_monotonic = 0.0
        self._last_fused_publish_monotonic = 0.0
        self._last_keyframe_transform: Optional[np.ndarray] = None
        self._keyframe_id = 0

        self._current_publisher = self.create_publisher(
            PointCloud2, "insight9_dense_map/current_points", 1
        )
        self._fused_publisher = self.create_publisher(
            PointCloud2, "insight9_dense_map/fused_points", 1
        )
        self._status_publisher = self.create_publisher(
            String, "insight9_dense_map/status", 1
        )
        self.create_subscription(
            PoseStamped, args.vio_topic, self._on_vio, qos_profile_sensor_data
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
        self.create_timer(0.5, self._publish)
        self.create_timer(0.5, self._resolve_extrinsic)
        self._worker.start()
        self.get_logger().info(
            "Insight9 dense StereoSGBM mapper started: "
            f"stride={args.pixel_stride}, voxel={args.voxel_size_m:.3f} m"
        )

    def destroy_node(self) -> bool:
        self._stop.set()
        try:
            self._work.put_nowait(None)  # type: ignore[arg-type]
        except queue.Full:
            pass
        self._worker.join(timeout=3.0)
        return super().destroy_node()

    def _resolve_extrinsic(self) -> None:
        if self._imu_to_left is not None:
            return
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
        self._imu_to_left = matrix_from_transform(
            (value.translation.x, value.translation.y, value.translation.z),
            (value.rotation.x, value.rotation.y, value.rotation.z, value.rotation.w),
        )
        self.get_logger().info(
            "resolved T_imu_left translation=(%.4f, %.4f, %.4f)"
            % tuple(self._imu_to_left[:3, 3])
        )

    def _on_vio(self, message: PoseStamped) -> None:
        pose = message.pose
        sample = PoseSample(
            stamp_ns=stamp_to_ns(message.header.stamp),
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
            matrix_from_pose(sample)
            reset = self._pose_buffer.append(sample)
        except ValueError as exc:
            self.get_logger().warning(f"rejected invalid VIO pose: {exc}")
            return
        if reset:
            with self._data_lock:
                self._voxel_map.clear()
                self._current_points = np.empty((0, 3), dtype=np.float32)
                self._keyframe_id = 0
                self._last_keyframe_transform = None
            self.get_logger().warning("VIO timestamp reset; cleared dense map")

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

    def _worker_main(self) -> None:
        period = 1.0 / max(self._args.dense_hz, 0.1)
        while not self._stop.is_set():
            try:
                pair = self._work.get(timeout=0.25)
            except queue.Empty:
                continue
            if pair is None:
                break
            if time.monotonic() - self._last_processed_monotonic < period:
                continue
            calibration = self._calibration
            imu_to_left = self._imu_to_left
            pose = self._pose_buffer.lookup(pair.stamp_ns)
            if calibration is None or imu_to_left is None or pose is None:
                if self._keyframe_id == 0:
                    self._latest_stats = {
                        "state": "waiting_for_calibration_tf_or_pose"
                    }
                continue
            started = time.perf_counter()
            try:
                left = image_to_mono8(pair.left)
                right = image_to_mono8(pair.right)
                result = self._estimator.reconstruct(left, right, calibration)
                world_to_left = compose_transform(matrix_from_pose(pose), imu_to_left)
                current_world = transform_points(
                    world_to_left, result.points_left
                ).astype(np.float32)
                added_voxels = 0
                keyframe = self._is_keyframe(world_to_left)
                with self._data_lock:
                    self._current_points = current_world
                    if keyframe:
                        self._keyframe_id += 1
                        added_voxels = self._voxel_map.update(current_world)
                        self._last_keyframe_transform = world_to_left
                    fused_voxels = len(self._voxel_map)
                self._last_processed_monotonic = time.monotonic()
                self._latest_stats = {
                    "state": "mapping",
                    "keyframe": self._keyframe_id,
                    "fused_this_frame": keyframe,
                    "valid_dense_pixels": result.valid_pixels,
                    "current_points": len(current_world),
                    "added_voxels": added_voxels,
                    "fused_voxels": fused_voxels,
                    "stereo_delta_ms": round(
                        abs(pair.left_stamp_ns - pair.right_stamp_ns) / 1_000_000.0,
                        3,
                    ),
                    "median_disparity_px": (
                        round(result.median_disparity_px, 3)
                        if result.median_disparity_px is not None
                        else None
                    ),
                    "median_depth_m": (
                        round(result.median_depth_m, 3)
                        if result.median_depth_m is not None
                        else None
                    ),
                    "processing_ms": round(
                        (time.perf_counter() - started) * 1000.0, 1
                    ),
                }
            except Exception as exc:
                self._latest_stats = {"state": "error", "error": str(exc)}
                self.get_logger().error(f"dense mapping frame failed: {exc}")

    def _publish(self) -> None:
        now = time.monotonic()
        publish_fused = (
            now - self._last_fused_publish_monotonic
            >= 1.0 / max(self._args.fused_publish_hz, 0.1)
        )
        with self._data_lock:
            current = self._current_points.copy()
            fused = self._voxel_map.points() if publish_fused else None
        header = Header()
        header.stamp = self.get_clock().now().to_msg()
        header.frame_id = self._map_frame
        self._current_publisher.publish(
            point_cloud2.create_cloud_xyz32(header, current.tolist())
        )
        if fused is not None:
            self._fused_publisher.publish(
                point_cloud2.create_cloud_xyz32(header, fused.tolist())
            )
            self._last_fused_publish_monotonic = now
        status = String()
        status.data = json.dumps(self._latest_stats, separators=(",", ":"))
        self._status_publisher.publish(status)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--left-image-topic", default="/insight9_a/camera/infra1/image_rect_raw")
    parser.add_argument("--right-image-topic", default="/insight9_a/camera/infra2/image_rect_raw")
    parser.add_argument("--left-info-topic", default="/insight9_a/camera/infra1/camera_info")
    parser.add_argument("--right-info-topic", default="/insight9_a/camera/infra2/camera_info")
    parser.add_argument("--vio-topic", default="/insight9_a/camera/vio_100hz")
    parser.add_argument("--imu-frame", default="insight9_a_camera_imu")
    parser.add_argument("--left-frame", default="insight9_a_camera_left")
    parser.add_argument("--map-frame", default="insight9_map")
    parser.add_argument("--dense-hz", type=float, default=2.0)
    parser.add_argument("--fused-publish-hz", type=float, default=1.0)
    parser.add_argument("--stereo-tolerance-ms", type=float, default=20.0)
    parser.add_argument("--num-disparities", type=int, default=128)
    parser.add_argument("--block-size", type=int, default=5)
    parser.add_argument("--min-disparity-px", type=float, default=1.0)
    parser.add_argument("--min-depth-m", type=float, default=0.25)
    parser.add_argument("--max-depth-m", type=float, default=6.0)
    parser.add_argument("--pixel-stride", type=int, default=2)
    parser.add_argument("--max-current-points", type=int, default=80_000)
    parser.add_argument("--voxel-size-m", type=float, default=0.04)
    parser.add_argument("--max-voxels", type=int, default=300_000)
    parser.add_argument("--keyframe-translation-m", type=float, default=0.05)
    parser.add_argument("--keyframe-rotation-deg", type=float, default=3.0)
    return parser


def main() -> int:
    args, ros_args = build_parser().parse_known_args()
    rclpy.init(args=ros_args)
    node: Optional[Insight9DenseMapper] = None
    try:
        node = Insight9DenseMapper(args)
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
