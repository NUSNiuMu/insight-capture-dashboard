#!/usr/bin/env python3

"""Localize both Insight3 cameras in the Insight9 SuperPoint map."""

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
    GlobalLocalizationConfig,
    IpcSuperGlueBackend,
    LocalizationConsensus,
    PoseBuffer,
    PoseSample,
    compose_transform,
    localize_features,
    matrix_from_pose,
    matrix_from_transform,
    rotation_distance_deg,
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
    from tf2_ros import Buffer, TransformBroadcaster, TransformListener
except ImportError as exc:  # pragma: no cover - exercised in the ROS image
    raise SystemExit(f"ROS 2 Python dependencies are unavailable: {exc}") from exc


CAMERAS = ("insight3_a", "insight3_b")


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
    ) -> None:
        self.name = name
        self.pose_buffer = PoseBuffer(max_bracket_gap_ns=50_000_000)
        self.history: deque[PoseSample] = deque(maxlen=history_points)
        self.latest_image: Optional[Image] = None
        self.camera_matrix: Optional[np.ndarray] = None
        self.imu_to_left: Optional[np.ndarray] = None
        self.consensus = LocalizationConsensus(config)
        self.last_image_stamp_ns = -1
        self.last_history_stamp_ns = -1
        self.next_process_monotonic = 0.0
        self.status: dict[str, object] = {"state": "waiting_for_inputs"}
        self.path = PathMsg()
        self.path.header.frame_id = "insight9_map"
        self.path_dirty = True
        self.transformed_history: deque[PoseStamped] = deque(maxlen=history_points)
        self.transformed_correction: Optional[np.ndarray] = None
        self.transformed_extrinsic: Optional[np.ndarray] = None
        self.last_transformed_stamp_ns = -1
        self.last_global_pose_publish_ns = -1


class Insight3GlobalLocalizer(Node):
    """Match both Insight3 streams to the shared Insight9 3D descriptor map."""

    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__("insight3_global_localizer")
        self._args = args
        self._map_frame = args.map_frame
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
        self._matcher = IpcSuperGlueBackend(Path(args.inference_socket))
        self._lock = threading.Lock()
        self._map_points = np.empty((0, 3), dtype=np.float32)
        self._map_descriptors = np.empty((0, 256), dtype=np.float32)
        self._cameras = {
            name: CameraState(name, config, history_points=args.path_points)
            for name in CAMERAS
        }
        self._path_publishers = {
            name: self.create_publisher(
                PathMsg, f"insight_global/{name}/path", 1
            )
            for name in CAMERAS
        }
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
        self._tf_broadcaster = TransformBroadcaster(self)
        self._tf_buffer = Buffer(cache_time=Duration(seconds=10.0))
        self._tf_listener = TransformListener(self._tf_buffer, self)
        self.create_subscription(
            PointCloud2,
            args.feature_map_topic,
            self._on_feature_map,
            1,
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
                ]
            )
            self.get_logger().info(f"{name} localization image <- {image_topic}")
        self._stop = threading.Event()
        self._worker = threading.Thread(
            target=self._worker_main, name="insight3-global-localization", daemon=True
        )
        self._worker.start()
        self.create_timer(0.5, self._resolve_extrinsics)
        self.create_timer(
            1.0 / max(args.path_publish_hz, 0.1), self._publish_paths_and_tf
        )
        self.create_timer(0.5, self._publish_status)
        self.get_logger().info(
            "SuperPoint global localizer started for insight3_a and insight3_b"
        )

    def _on_reset(self, _request: Empty.Request, response: Empty.Response) -> Empty.Response:
        with self._lock:
            self._map_points = np.empty((0, 3), dtype=np.float32)
            self._map_descriptors = np.empty((0, 256), dtype=np.float32)
            for state in self._cameras.values():
                state.history.clear()
                state.consensus = LocalizationConsensus(state.consensus.config)
                state.last_image_stamp_ns = -1
                state.last_history_stamp_ns = -1
                state.transformed_history.clear()
                state.transformed_correction = None
                state.transformed_extrinsic = None
                state.last_transformed_stamp_ns = -1
                state.last_global_pose_publish_ns = -1
                state.path = PathMsg()
                state.path.header.frame_id = self._map_frame
                state.path_dirty = True
                state.status = {"state": "waiting_for_map", "localized": False}
        self.get_logger().info("Cleared Insight3 global corrections for a new map")
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
        with self._lock:
            self._map_points = points
            self._map_descriptors = descriptors

    def _image_callback(self, name: str):
        def callback(message: Image) -> None:
            with self._lock:
                self._cameras[name].latest_image = message

        return callback

    def _camera_info_callback(self, name: str):
        def callback(message: CameraInfo) -> None:
            projection = np.asarray(message.p, dtype=np.float64).reshape(3, 4)
            matrix = projection[:, :3]
            if matrix[0, 0] <= 0.0 or matrix[1, 1] <= 0.0:
                return
            with self._lock:
                self._cameras[name].camera_matrix = matrix

        return callback

    def _vio_callback(self, name: str):
        def callback(message: PoseStamped) -> None:
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
            state = self._cameras[name]
            global_pose = None
            with self._lock:
                try:
                    matrix_from_pose(sample)
                    reset = state.pose_buffer.append(sample)
                except ValueError:
                    return
                if reset:
                    state.history.clear()
                    state.consensus = LocalizationConsensus(state.consensus.config)
                    state.last_image_stamp_ns = -1
                    state.last_history_stamp_ns = -1
                    state.transformed_history.clear()
                    state.transformed_correction = None
                    state.transformed_extrinsic = None
                    state.last_transformed_stamp_ns = -1
                    state.last_global_pose_publish_ns = -1
                    state.path = PathMsg()
                    state.path.header.frame_id = self._map_frame
                    state.path_dirty = True
                if (
                    sample.stamp_ns - state.last_history_stamp_ns
                    >= self._args.path_interval_ms * 1_000_000
                ):
                    state.history.append(sample)
                    state.last_history_stamp_ns = sample.stamp_ns
                    state.path_dirty = True
                correction = (
                    None
                    if state.transformed_correction is None
                    else state.transformed_correction.copy()
                )
                extrinsic = (
                    None
                    if state.transformed_extrinsic is None
                    else state.transformed_extrinsic.copy()
                )
                publish_interval_ns = int(
                    1_000_000_000 / max(self._args.pose_publish_hz, 0.1)
                )
                should_publish = (
                    sample.stamp_ns - state.last_global_pose_publish_ns
                    >= publish_interval_ns
                )
                if should_publish:
                    state.last_global_pose_publish_ns = sample.stamp_ns
            if should_publish and correction is not None and extrinsic is not None:
                transform = correction @ matrix_from_pose(sample) @ extrinsic
                global_pose = pose_message(
                    transform, message.header.stamp, self._map_frame
                )
            if global_pose is not None:
                self._pose_publishers[name].publish(global_pose)

        return callback

    def _resolve_extrinsics(self) -> None:
        for name, state in self._cameras.items():
            if state.imu_to_left is not None:
                continue
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
                (value.rotation.x, value.rotation.y, value.rotation.z, value.rotation.w),
            )
            with self._lock:
                state.imu_to_left = imu_to_left
            self.get_logger().info(f"resolved {name} T_imu_left")

    def _worker_main(self) -> None:
        period = 1.0 / max(self._args.localization_hz, 0.1)
        while not self._stop.wait(0.05):
            for name, state in self._cameras.items():
                now = time.monotonic()
                if now < state.next_process_monotonic:
                    continue
                with self._lock:
                    message = state.latest_image
                    camera_matrix = (
                        None
                        if state.camera_matrix is None
                        else state.camera_matrix.copy()
                    )
                    imu_to_left = (
                        None if state.imu_to_left is None else state.imu_to_left.copy()
                    )
                    map_points = self._map_points.copy()
                    map_descriptors = self._map_descriptors.copy()
                if (
                    message is None
                    or camera_matrix is None
                    or imu_to_left is None
                    or len(map_points) < self._args.min_map_features
                ):
                    localized = state.consensus.correction is not None
                    state.status = {
                        "state": "localized" if localized else "waiting",
                        "localized": localized,
                        "image_ready": message is not None,
                        "camera_info_ready": camera_matrix is not None,
                        "extrinsic_ready": imu_to_left is not None,
                        "map_features": len(map_points),
                        "need_map_features": self._args.min_map_features,
                    }
                    state.next_process_monotonic = now + 0.5
                    continue
                stamp_ns = stamp_to_ns(message.header.stamp)
                if stamp_ns <= state.last_image_stamp_ns:
                    continue
                with self._lock:
                    pose = state.pose_buffer.lookup(stamp_ns)
                if pose is None:
                    continue
                state.last_image_stamp_ns = stamp_ns
                state.next_process_monotonic = now + period
                started = time.perf_counter()
                try:
                    image = grayscale_image(message)
                    features = self._matcher.extract(image)
                    odom_to_left = compose_transform(
                        matrix_from_pose(pose), imu_to_left
                    )
                    candidate, diagnostics = localize_features(
                        features.keypoints,
                        features.descriptors,
                        map_points,
                        map_descriptors,
                        camera_matrix,
                        odom_to_left,
                        image.shape,
                        state.consensus.config,
                    )
                    with self._lock:
                        transition = state.consensus.observe(candidate)
                        if transition["localized"]:
                            state.path_dirty = True
                    state.status = {
                        "state": (
                            "localized"
                            if transition["localized"]
                            else "localizing"
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
                    with self._lock:
                        state.consensus.observe(None)
                    state.status = {"state": "error", "error": str(exc)}
                    self.get_logger().error(f"{name} localization failed: {exc}")

    def _rebuild_path(self, state: CameraState) -> None:
        with self._lock:
            correction = (
                None
                if state.consensus.correction is None
                else state.consensus.correction.copy()
            )
            extrinsic = (
                None if state.imu_to_left is None else state.imu_to_left.copy()
            )
            history = tuple(state.history)
            state.path_dirty = False
        if correction is None or extrinsic is None:
            return

        if state.transformed_correction is None:
            correction_changed = True
        else:
            correction_translation_m = float(
                np.linalg.norm(
                    correction[:3, 3] - state.transformed_correction[:3, 3]
                )
            )
            correction_rotation_deg = rotation_distance_deg(
                state.transformed_correction, correction
            )
            correction_changed = (
                correction_translation_m
                >= self._args.path_reset_translation_m
                or correction_rotation_deg
                >= self._args.path_reset_rotation_deg
            )
        extrinsic_changed = (
            state.transformed_extrinsic is None
            or not np.allclose(
                extrinsic, state.transformed_extrinsic, rtol=0.0, atol=1e-12
            )
        )
        if correction_changed or extrinsic_changed:
            state.transformed_history.clear()
            state.last_transformed_stamp_ns = -1
            state.last_global_pose_publish_ns = -1
            pending = history[-1:]
            with self._lock:
                state.history.clear()
                state.history.extend(pending)
            active_correction = correction
        else:
            pending = tuple(
                sample
                for sample in history
                if sample.stamp_ns > state.last_transformed_stamp_ns
            )
            active_correction = state.transformed_correction

        for sample in pending:
            transform = active_correction @ matrix_from_pose(sample) @ extrinsic
            stamp = Time(nanoseconds=sample.stamp_ns).to_msg()
            state.transformed_history.append(
                pose_message(transform, stamp, self._map_frame)
            )
            state.last_transformed_stamp_ns = sample.stamp_ns

        if correction_changed or extrinsic_changed:
            state.transformed_correction = correction
        state.transformed_extrinsic = extrinsic
        path = PathMsg()
        path.header.frame_id = self._map_frame
        path.poses = list(state.transformed_history)
        if path.poses:
            path.header.stamp = path.poses[-1].header.stamp
        state.path = path

    def _publish_paths_and_tf(self) -> None:
        now_stamp = self.get_clock().now().to_msg()
        for name, state in self._cameras.items():
            if state.path_dirty:
                self._rebuild_path(state)
            if state.path.poses:
                self._path_publishers[name].publish(state.path)
                latest = state.path.poses[-1]
                transform = TransformStamped()
                transform.header.frame_id = self._map_frame
                transform.header.stamp = now_stamp
                transform.child_frame_id = f"{name}_global_camera_left"
                transform.transform.translation.x = latest.pose.position.x
                transform.transform.translation.y = latest.pose.position.y
                transform.transform.translation.z = latest.pose.position.z
                transform.transform.rotation = latest.pose.orientation
                self._tf_broadcaster.sendTransform(transform)

    def _publish_status(self) -> None:
        for name, state in self._cameras.items():
            status = dict(state.status)
            status["camera"] = name
            status["history_points"] = len(state.history)
            status["published_path_points"] = len(state.path.poses)
            message = String()
            message.data = json.dumps(status, separators=(",", ":"))
            self._status_publishers[name].publish(message)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inference-socket", default="/run/superglue/matcher.sock")
    parser.add_argument(
        "--feature-map-topic", default="/insight9_sparse_map/features"
    )
    parser.add_argument(
        "--image-topic-template",
        default="/insight_mapping/{name}/infra1/image_rect_raw",
    )
    parser.add_argument("--map-frame", default="insight9_map")
    parser.add_argument("--localization-hz", type=float, default=1.0)
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
    parser.add_argument("--path-publish-hz", type=float, default=5.0)
    parser.add_argument("--pose-publish-hz", type=float, default=50.0)
    parser.add_argument("--path-reset-translation-m", type=float, default=0.05)
    parser.add_argument("--path-reset-rotation-deg", type=float, default=3.0)
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
