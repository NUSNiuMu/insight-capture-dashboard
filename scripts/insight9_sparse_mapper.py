#!/usr/bin/env python3

"""Build a session-local Insight9 sparse stereo map and publish it for RViz."""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional, Tuple

import cv2
import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from insight9_mapping_core import (  # noqa: E402
    IpcSuperGlueBackend,
    LandmarkMap,
    LandmarkMapConfig,
    OfficialSuperGlueBackend,
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
    triangulate_rectified,
)

try:
    import rclpy
    from builtin_interfaces.msg import Time as TimeMsg
    from geometry_msgs.msg import PoseStamped, TransformStamped
    from nav_msgs.msg import Path as PathMsg
    from rclpy.duration import Duration
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data
    from rclpy.time import Time
    from sensor_msgs.msg import CameraInfo, Image, PointCloud2
    from sensor_msgs_py import point_cloud2
    from std_msgs.msg import Header, String
    from tf2_ros import Buffer, TransformBroadcaster, TransformListener
except ImportError as exc:  # pragma: no cover - exercised inside the ROS image
    raise SystemExit(f"ROS 2 Python dependencies are unavailable: {exc}") from exc


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


class Insight9SparseMapper(Node):
    """Coordinate lightweight ROS callbacks with a single inference worker."""

    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__("insight9_sparse_mapper")
        self._args = args
        self._map_frame = args.map_frame
        self._camera_frame = args.mapping_camera_frame
        self._pose_buffer = PoseBuffer(max_bracket_gap_ns=50_000_000)
        self._stereo_sync: StereoPairSynchronizer[Image] = StereoPairSynchronizer(
            tolerance_ns=int(args.stereo_tolerance_ms * 1_000_000)
        )
        self._landmarks = LandmarkMap(
            LandmarkMapConfig(
                voxel_size_m=args.voxel_size_m,
                confirmation_observations=args.confirmation_observations,
                candidate_ttl_keyframes=args.candidate_ttl_keyframes,
                max_landmarks=args.max_landmarks,
            )
        )
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
        self._tf_buffer = Buffer(cache_time=Duration(seconds=10.0))
        self._tf_listener = TransformListener(self._tf_buffer, self)
        self._tf_broadcaster = TransformBroadcaster(self)
        self._work: queue.Queue[StereoPair[Image]] = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._worker = threading.Thread(
            target=self._worker_main, name="superglue-mapping", daemon=True
        )
        self._last_processed_monotonic = 0.0
        self._last_keyframe_transform: Optional[np.ndarray] = None
        self._keyframe_id = 0
        self._path = deque(maxlen=args.path_points)
        self._path_lock = threading.Lock()
        self._map_lock = threading.Lock()
        self._last_path_append_ns = 0
        self._latest_stats = {"state": "waiting_for_inputs"}

        self._pointcloud_publisher = self.create_publisher(
            PointCloud2, "insight9_sparse_map/points", 1
        )
        self._path_publisher = self.create_publisher(
            PathMsg, "insight9_sparse_map/path", 1
        )
        self._status_publisher = self.create_publisher(
            String, "insight9_sparse_map/status", 1
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
        self.create_timer(0.5, self._publish_map)
        self.create_timer(0.05, self._publish_path_and_tf)
        self.create_timer(0.5, self._resolve_extrinsic)
        self._worker.start()
        self.get_logger().info(
            "official SuperPoint/SuperGlue validation mapper started; "
            "the licensed model image is internal-validation only"
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
            with self._path_lock:
                self._path.clear()
            with self._map_lock:
                self._landmarks.clear()
            self._last_keyframe_transform = None
            self._keyframe_id = 0
            self.get_logger().warning("VIO timestamp reset; cleared session map")
        if self._imu_to_left is None:
            return
        world_to_left = compose_transform(matrix_from_pose(sample), self._imu_to_left)
        if sample.stamp_ns - self._last_path_append_ns >= self._args.path_interval_ms * 1_000_000:
            stamped = matrix_to_pose_stamped(world_to_left, sample.stamp_ns, self._map_frame)
            with self._path_lock:
                self._path.append(stamped)
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
            elapsed = time.monotonic() - self._last_processed_monotonic
            if elapsed < period:
                continue
            calibration = self._calibration
            imu_to_left = self._imu_to_left
            pose = self._pose_buffer.lookup(pair.stamp_ns)
            if calibration is None or imu_to_left is None or pose is None:
                self._latest_stats = {"state": "waiting_for_calibration_tf_or_pose"}
                continue
            world_to_left = compose_transform(matrix_from_pose(pose), imu_to_left)
            if not self._is_keyframe(world_to_left):
                continue
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
                world_points = transform_points(world_to_left, triangulated.points_left)
                self._keyframe_id += 1
                with self._map_lock:
                    update = self._landmarks.update(
                        self._keyframe_id,
                        world_points,
                        descriptors=matches.descriptors[source],
                        scores=matches.scores[source],
                    )
                self._last_keyframe_transform = world_to_left
                self._last_processed_monotonic = time.monotonic()
                self._latest_stats = {
                    "state": "mapping",
                    "keyframe": self._keyframe_id,
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
                    "backend_inference_ms": matches.backend_inference_ms,
                    "inference_and_geometry_ms": round(
                        (time.perf_counter() - started) * 1000.0, 1
                    ),
                }
            except Exception as exc:
                self._latest_stats = {"state": "error", "error": str(exc)}
                self.get_logger().error(f"mapping frame failed: {exc}")

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

    def _publish_map(self) -> None:
        with self._map_lock:
            points = self._landmarks.points()
        header = Header()
        header.stamp = self.get_clock().now().to_msg()
        header.frame_id = self._map_frame
        cloud = point_cloud2.create_cloud_xyz32(header, points.tolist())
        self._pointcloud_publisher.publish(cloud)
        status = String()
        status.data = json.dumps(self._latest_stats, separators=(",", ":"))
        self._status_publisher.publish(status)

    def _publish_path_and_tf(self) -> None:
        with self._path_lock:
            poses = list(self._path)
        if not poses:
            return
        path = PathMsg()
        path.header.stamp = poses[-1].header.stamp
        path.header.frame_id = self._map_frame
        path.poses = poses
        self._path_publisher.publish(path)

        latest = poses[-1]
        transform = TransformStamped()
        transform.header.frame_id = self._map_frame
        transform.header.stamp = self.get_clock().now().to_msg()
        transform.child_frame_id = self._camera_frame
        transform.transform.translation.x = latest.pose.position.x
        transform.transform.translation.y = latest.pose.position.y
        transform.transform.translation.z = latest.pose.position.z
        transform.transform.rotation = latest.pose.orientation
        self._tf_broadcaster.sendTransform(transform)


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--superglue-checkout",
        default=str(root / "data" / "models" / "SuperGluePretrainedNetwork"),
    )
    parser.add_argument("--backend", choices=("ipc", "official-torch"), default="ipc")
    parser.add_argument("--inference-socket", default="/run/superglue/matcher.sock")
    parser.add_argument("--superglue-weights", choices=("indoor", "outdoor"), default="indoor")
    parser.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    parser.add_argument("--left-image-topic", default="/insight9_a/camera/infra1/image_rect_raw")
    parser.add_argument("--right-image-topic", default="/insight9_a/camera/infra2/image_rect_raw")
    parser.add_argument("--left-info-topic", default="/insight9_a/camera/infra1/camera_info")
    parser.add_argument("--right-info-topic", default="/insight9_a/camera/infra2/camera_info")
    parser.add_argument("--vio-topic", default="/insight9_a/camera/vio_100hz")
    parser.add_argument("--imu-frame", default="insight9_a_camera_imu")
    parser.add_argument("--left-frame", default="insight9_a_camera_left")
    parser.add_argument("--map-frame", default="insight9_map")
    parser.add_argument("--mapping-camera-frame", default="insight9_mapping_camera_left")
    parser.add_argument("--mapping-hz", type=float, default=5.0)
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
    parser.add_argument("--candidate-ttl-keyframes", type=int, default=12)
    parser.add_argument("--max-landmarks", type=int, default=100_000)
    parser.add_argument("--path-points", type=int, default=200)
    parser.add_argument("--path-interval-ms", type=int, default=50)
    return parser


def main() -> int:
    args, ros_args = build_parser().parse_known_args()
    rclpy.init(args=ros_args)
    node: Optional[Insight9SparseMapper] = None
    try:
        node = Insight9SparseMapper(args)
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.destroy_node()
        rclpy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
