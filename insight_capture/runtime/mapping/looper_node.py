"""ROS transport for the serialized LooperVIO map and independent wrist VIOs."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import queue
import threading
import time
import uuid
from types import SimpleNamespace

import numpy as np
from scipy.spatial.transform import Rotation
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data, QoSProfile, DurabilityPolicy, ReliabilityPolicy
from rclpy.time import Time
from geometry_msgs.msg import PoseStamped, TransformStamped
from sensor_msgs.msg import CameraInfo, Image, PointCloud2, PointField
from std_msgs.msg import String, Header
from std_srvs.srv import Empty
from tf2_msgs.msg import TFMessage
from tf2_ros import Buffer, TransformBroadcaster, TransformListener, StaticTransformBroadcaster

from .geometry import PoseSample, matrix_from_pose, matrix_from_transform
from .looper_backend import LooperBackend
from .looper_features import stereo_arrays, query_arrays
from .superglue_backend import IpcSuperGlueBackend
from .rig import LatestViewQueue
from .synchronization import PoseBuffer, SourceClock, StereoPairSynchronizer, select_timestamp
from insight_capture.core.paths import runtime_config_path
from insight_capture.core.localization_settings import load_gripper_mask_height_ratio


def stamp_ns(stamp):
    return stamp.sec * 1_000_000_000 + stamp.nanosec


def gray(message):
    if message.encoding.lower() not in {"mono8", "8uc1", "nv12"}:
        raise ValueError(f"Unsupported mapping image: {message.encoding}")
    raw = np.frombuffer(message.data, np.uint8)
    rows = message.height
    if message.encoding.lower() == "nv12":
        rows = raw.size * 2 // (3 * message.step)
    return raw[:rows * message.step].reshape(rows, message.step)[:, :message.width].copy()


@dataclass
class CameraState:
    buffer: PoseBuffer = field(default_factory=PoseBuffer)
    correction: object = None
    info: object = None
    center: object = None
    next_image: int = 0
    clock: SourceClock = field(default_factory=SourceClock)
    latest_source_ns: int = 0
    last_vio_received_ns: int = 0
    tracking: str = "UNKNOWN"
    diagnostics: dict = field(default_factory=dict)
    last_match: float = 0.
    map_revision: int = 0
    tracking_epoch: int = 0


class LooperNode(Node):
    def __init__(self, args):
        super().__init__("insight_looper_mapping")
        config = json.loads(args.config.read_text())
        self.runtime_config = args.runtime_config
        load_gripper_mask_height_ratio(self.runtime_config)
        self.head = next(c["name"] for c in config["cameras"] if c.get("teleop_role") == "head")
        self.names = (self.head, "insight3_a", "insight3_b")
        self.inputs = {name: {
            "left_info_topic": f"/{name}/camera/infra1/camera_info",
            "left_image_topic": f"/{name}/camera/infra1/image_rect_raw",
            "vio_topic": f"/{name}/camera/vio_100hz",
            "imu_frame": name + "_camera_imu",
            "center_left_frame": name + "_camera_left",
            "center_right_frame": name + "_camera_right",
        } for name in self.names[1:]}
        self.inputs[self.head] = config["mapping"]
        self.cameras = {name: CameraState() for name in self.names}
        self.frames = {name: name + "_global_camera_center" for name in self.names}
        self.frames[self.head] = self.inputs[self.head]["mapping_camera_frame"]
        self.backend = LooperBackend(args.socket)
        self.features = IpcSuperGlueBackend(args.feature_socket)
        self.lock = threading.RLock()
        self.processing = threading.Lock()
        self.work = LatestViewQueue(self.names)
        self.stop = threading.Event()
        self.sync = StereoPairSynchronizer(tolerance_ns=2_000_000)
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)
        self.tf_broadcaster = TransformBroadcaster(self)
        self.static_tf = StaticTransformBroadcaster(self)
        self.geometry_id = None
        self.static_transforms = {}
        self.ready = False
        self.failed = False
        self.generation = 0
        self.session_id = uuid.uuid4().hex
        self.last_reset_wall_ns = 0
        self.last_map_source_ns = 0
        self.stats = {"state": "waiting_for_inputs", "map_point_count": 0}
        self.right_info = None
        self.pose_publishers = {}
        self.status_publishers = {}
        geometry_qos = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                                 reliability=ReliabilityPolicy.RELIABLE)
        self.create_subscription(TFMessage, "/tf_static", self.on_static_tf,
                                 QoSProfile(depth=100, durability=DurabilityPolicy.TRANSIENT_LOCAL))
        self.feature_publisher = self.create_publisher(PointCloud2, "/insight9_sparse_map/features", geometry_qos)
        base = "/insight9_sparse_map/calibration_keyframe"
        self.reference_image = self.create_publisher(Image, base + "/image", geometry_qos)
        self.reference_points = self.create_publisher(PointCloud2, base + "/points", geometry_qos)
        self.last_geometry = 0.
        self.last_pose_tick = None
        for camera in config["cameras"]:
            calibration = camera.get("camera_center_to_tcp")
            if calibration is None:
                continue
            tf = TransformStamped()
            tf.header.frame_id = camera["name"] + "_global_camera_center"
            tf.child_frame_id = camera["tcp_frame_id"]
            tf.transform.translation.x, tf.transform.translation.y, tf.transform.translation.z = map(float, calibration["translation_m"])
            tf.transform.rotation.x, tf.transform.rotation.y, tf.transform.rotation.z, tf.transform.rotation.w = map(float, calibration["rotation_xyzw"])
            self.static_tf.sendTransform(tf)
        for name in self.names:
            base = "/insight9_sparse_map" if name == self.head else f"/insight_global/{name}"
            self.pose_publishers[name] = self.create_publisher(PoseStamped, base + "/pose", 1)
            self.status_publishers[name] = self.create_publisher(String, base + "/status", 1)
            inputs = self.inputs[name]
            self.create_subscription(CameraInfo, inputs["left_info_topic"],
                                     lambda m, n=name: setattr(self.cameras[n], "info", m), qos_profile_sensor_data)
            self.create_subscription(PoseStamped, inputs["vio_topic"],
                                     lambda m, n=name: self.on_vio(n, m), qos_profile_sensor_data)
            self.create_subscription(String, f"/{name}/camera/vio_status",
                                     lambda m, n=name: self.on_tracking(n, m.data), 1)
            self.create_subscription(Image, inputs["left_image_topic"],
                                     lambda m, n=name: self.on_image(n, 0, m), qos_profile_sensor_data)
        self.create_subscription(CameraInfo, self.inputs[self.head]["right_info_topic"],
                                 lambda m: setattr(self, "right_info", m), qos_profile_sensor_data)
        self.create_subscription(Image, self.inputs[self.head]["right_image_topic"],
                                 lambda m: self.on_image(self.head, 1, m), qos_profile_sensor_data)
        self.create_service(Empty, "/insight_global/reset", self.reset_map)
        self.create_timer(.5, self.publish_status)
        self.create_timer(1. / 30., self.publish_poses)
        self.worker = threading.Thread(target=self.run, name="looper-owner", daemon=True)
        self.worker.start()

    def lookup(self, target, source):
        t = self.tf_buffer.lookup_transform(target, source, Time()).transform
        return matrix_from_transform((t.translation.x, t.translation.y, t.translation.z),
                                     (t.rotation.x, t.rotation.y, t.rotation.z, t.rotation.w))

    def on_static_tf(self, message):
        prefixes = tuple(name + "_" for name in self.names)
        with self.lock:
            for tf in message.transforms:
                if not tf.child_frame_id.startswith(prefixes):
                    continue
                t, q = tf.transform.translation, tf.transform.rotation
                value = (tf.header.frame_id, t.x, t.y, t.z, q.x, q.y, q.z, q.w)
                if self.static_transforms.get(tf.child_frame_id) != value:
                    self.static_transforms[tf.child_frame_id] = value
                    self.geometry_id = None

    def configure(self):
        infos = [self.cameras[self.head].info, self.right_info,
                 self.cameras["insight3_a"].info, self.cameras["insight3_b"].info]
        if any(info is None for info in infos):
            return False
        with self.lock:
            identity = (tuple((info.width, info.height, tuple(info.p), tuple(info.r), info.header.frame_id)
                              for info in infos), tuple(sorted(self.static_transforms.items())))
        if identity == self.geometry_id:
            return True
        required = [(self.inputs[name]["imu_frame"], frame)
                    for name in self.names for frame in (
                        self.cameras[name].info.header.frame_id,
                        self.inputs[name]["center_left_frame"],
                        self.inputs[name]["center_right_frame"])]
        required.append((self.inputs[self.head]["imu_frame"], self.right_info.header.frame_id))
        if not all(self.tf_buffer.can_transform(target, source, Time()) for target, source in required):
            return False
        models = []
        centers = {}
        for i, (name, info) in enumerate(zip((self.head, self.head, *self.names[1:]), infos)):
            inputs = self.inputs[name]
            imu = inputs["imu_frame"]
            left_info = self.cameras[name].info
            extrinsic = self.lookup(imu, left_info.header.frame_id)
            rectified = np.eye(4)
            rectified[:3, :3] = np.asarray(left_info.r).reshape(3, 3).T
            extrinsic = extrinsic @ rectified
            P = np.asarray(info.p).reshape(3, 4)
            K = P[:, :3]
            if i == 1:
                relative = np.eye(4)
                relative[:3, 3] = -np.linalg.solve(K, P[:, 3])
                extrinsic = extrinsic @ relative
            models.append({"width": info.width, "height": info.height,
                           "intrinsics": [K[0, 0], K[1, 1], K[0, 2], K[1, 2]],
                           "imu_from_camera": extrinsic.tolist()})
            left = self.lookup(imu, inputs["center_left_frame"])
            right = self.lookup(imu, inputs["center_right_frame"])
            center = left.copy()
            center[:3, 3] = (left[:3, 3] + right[:3, 3]) / 2
            centers[name] = center
        if identity != self.geometry_id:
            with self.processing, self.lock:
                self.clear()
                self.backend.request("initialize", cameras=models)
                for name, center in centers.items():
                    self.cameras[name].center = center
                self.geometry_id = identity
                self.ready = True
            self.get_logger().info("LooperVIO geometry initialized from live CameraInfo and TF")
        return True

    def clear(self):
        self.failed = False
        self.generation += 1
        self.last_reset_wall_ns = time.time_ns()
        self.last_map_source_ns = 0
        self.work.clear()
        self.sync.clear()
        self.last_geometry = 0.
        self.last_pose_tick = None
        for state in self.cameras.values():
            state.correction = None
            state.next_image = 0
            state.diagnostics = {}
            state.last_match = 0.
            state.map_revision = 0
        self.stats = {"state": "building", "map_point_count": 0}
        self.publish_geometry(None, [np.empty((0, 259), np.float32), np.empty((0, 5), np.float32)])

    def reset_map(self, request, response):
        with self.processing, self.lock:
            if self.ready:
                self.backend.request("reset")
            self.clear()
        return response

    def on_image(self, name, eye, message):
        if self.cameras[name].tracking not in {"TRACKING", "TRACKING_STATIC"}:
            return
        stamp = stamp_ns(message.header.stamp)
        if name == self.head:
            pair = (self.sync.push_left if eye == 0 else self.sync.push_right)(stamp, message)
            if pair is None:
                return
            stamp = pair.stamp_ns
            item = SimpleNamespace(view=name, stamp_ns=stamp, images=(pair.left, pair.right))
        else:
            item = SimpleNamespace(view=name, stamp_ns=stamp, images=(message,))
        state = self.cameras[name]
        due, state.next_image = select_timestamp(stamp, state.next_image, 2.)
        if due:
            item.generation = self.generation
            item.tracking_epoch = state.tracking_epoch
            self.work.put_nowait(item)

    def on_tracking(self, name, tracking):
        with self.lock:
            state = self.cameras[name]
            previous = state.tracking
            state.tracking = tracking
            invalidated = previous in {"TRACKING", "TRACKING_STATIC"} and tracking not in {"TRACKING", "TRACKING_STATIC"}
        if invalidated:
            self.invalidate_camera(name)

    def invalidate_camera(self, name):
        with self.lock:
            state = self.cameras[name]
            state.correction = None
            state.tracking_epoch += 1
            state.buffer.clear()
            state.clock = SourceClock()
            state.latest_source_ns = state.last_vio_received_ns = 0
            state.last_match = 0.
            state.diagnostics = {"state": "waiting_for_vio", "geometric_match": False}
        if name == self.head:
            self.reset_map(None, Empty.Response())

    def on_vio(self, name, message):
        p = message.pose
        sample = PoseSample(stamp_ns(message.header.stamp),
                            np.array([p.position.x, p.position.y, p.position.z]),
                            np.array([p.orientation.x, p.orientation.y, p.orientation.z, p.orientation.w]))
        state = self.cameras[name]
        received = self.get_clock().now().nanoseconds
        if state.last_vio_received_ns and not 0 <= received - state.last_vio_received_ns <= 2_000_000_000:
            self.invalidate_camera(name)
        if state.buffer.append(sample):
            self.reset_map(None, Empty.Response())
            state.clock = SourceClock()
        with self.lock:
            state.clock.observe(sample.stamp_ns, received)
            state.latest_source_ns = sample.stamp_ns
            state.last_vio_received_ns = received

    def publish_poses(self):
        with self.lock:
            states = list(self.cameras.values())
            if not self.ready or any(s.correction is None or not s.clock.ready
                    or s.map_revision != states[0].map_revision
                    or s.tracking not in {"TRACKING", "TRACKING_STATIC"} for s in states):
                self.last_pose_tick = None
                return
            latest = min(s.latest_source_ns + s.clock.offset_ns for s in states)
            latest_tick = latest * 30 // 1_000_000_000
            first_tick = latest_tick if self.last_pose_tick is None else self.last_pose_tick + 1
            for tick in range(first_tick, latest_tick + 1):
                stamp = tick * 1_000_000_000 // 30
                poses = [s.buffer.lookup(stamp - s.clock.offset_ns) for s in states]
                if any(p is None for p in poses):
                    self.last_pose_tick = None
                    return
                self.last_pose_tick = tick
                transforms = []
                for name, state, pose in zip(self.names, states, poses):
                    transform = state.correction @ matrix_from_pose(pose) @ state.center
                    output = PoseStamped()
                    output.header.stamp = Time(nanoseconds=stamp).to_msg()
                    output.header.frame_id = "insight9_map"
                    output.pose.position.x, output.pose.position.y, output.pose.position.z = map(float, transform[:3, 3])
                    q = Rotation.from_matrix(transform[:3, :3]).as_quat()
                    output.pose.orientation.x, output.pose.orientation.y, output.pose.orientation.z, output.pose.orientation.w = map(float, q)
                    self.pose_publishers[name].publish(output)
                    tf = TransformStamped(header=output.header, child_frame_id=self.frames[name])
                    tf.transform.translation.x, tf.transform.translation.y, tf.transform.translation.z = map(float, transform[:3, 3])
                    tf.transform.rotation = output.pose.orientation
                    transforms.append(tf)
                self.tf_broadcaster.sendTransform(transforms)

    def request(self, item, command, **metadata):
        with self.processing:
            if (item.generation != self.generation
                    or item.tracking_epoch != self.cameras[item.view].tracking_epoch
                    or item.stamp_ns + self.cameras[item.view].clock.offset_ns < self.last_reset_wall_ns):
                return None
            return self.backend.request(command, **metadata)

    def publish_geometry(self, image, arrays):
        header = Header(frame_id="insight9_map")
        header.stamp = (Time(nanoseconds=self.last_reset_wall_ns).to_msg() if image is None else
                        Time(nanoseconds=stamp_ns(image.header.stamp) + self.cameras[self.head].clock.offset_ns).to_msg())
        for publisher, values, fields in (
            (self.feature_publisher, arrays[0], [("x", 1), ("y", 1), ("z", 1), ("descriptor", 256)]),
            (self.reference_points, arrays[1], [("x", 1), ("y", 1), ("z", 1), ("u", 1), ("v", 1)]),
        ):
            cloud = PointCloud2(header=header, height=1, width=len(values), is_dense=True)
            offset = 0
            for name, count in fields:
                cloud.fields.append(PointField(name=name, offset=offset, datatype=PointField.FLOAT32, count=count))
                offset += count * 4
            cloud.point_step = offset
            cloud.row_step = len(values) * offset
            cloud.data = np.asarray(values, np.float32).tobytes()
            publisher.publish(cloud)
        if image is not None:
            data = gray(image)
            reference = Image(header=header, height=data.shape[0], width=data.shape[1],
                              encoding="mono8", step=data.shape[1], data=data.tobytes())
            self.reference_image.publish(reference)

    def commit_result(self, item, result, O):
        state = self.cameras[item.view]
        with self.lock:
            if (result is None or item.generation != self.generation
                    or item.tracking_epoch != state.tracking_epoch):
                return False
            if item.view == self.head:
                state.correction = (np.asarray(result["map_from_imu"]) @ np.linalg.inv(O)
                                    if result["mapped"] else None)
                state.map_revision = result["map_revision"]
                self.last_map_source_ns = item.stamp_ns
                for update in result["wrist_updates"]:
                    wrist = self.cameras[self.names[int(update["camera_id"]) - 1]]
                    if update["tracking_epoch"] != wrist.tracking_epoch:
                        continue
                    wrist.correction = (np.asarray(update["map_from_imu"]) @ np.linalg.inv(update["odom_from_imu"])) if update["localized"] else None
                    if not update["localized"]:
                        wrist.diagnostics = {"state": "searching", "geometric_match": False,
                                             "reason": "insufficient_retained_landmarks"}
                    wrist.map_revision = update["map_revision"]
                self.stats = {"state": "mapping" if result["mapped"] else "building", "map_point_count": result["landmarks"],
                              "keyframes": result["keyframes"], "raw_landmarks": result["raw_landmarks"]}
            else:
                M = np.asarray(result["map_from_imu"]) if result["localized"] else None
                if M is not None:
                    state.correction = M @ np.linalg.inv(O)
                    state.last_match = time.monotonic()
                state.map_revision = result["map_revision"]
                state.diagnostics = {"state": "tracking" if state.correction is not None else "searching",
                                     "geometric_match": result["localized"], "confirmed": result["localized"],
                                     "reason": result.get("reason"), **{key: result[key] for key in
                                      ("matches", "inliers", "grid_cells", "reprojection_rms_px", "gripper_mask_height_ratio") if key in result}}
            return True

    def run(self):
        while not self.stop.is_set():
            try:
                item = self.work.get(timeout=.25)
                if item is None:
                    break
                if self.failed or not self.configure() or item.generation != self.generation:
                    continue
                state = self.cameras[item.view]
                infos = (state.info, self.right_info)
                if any(image.header.frame_id != info.header.frame_id
                       for image, info in zip(item.images, infos)):
                    continue
                deadline = time.monotonic() + .1
                pose = state.buffer.lookup(item.stamp_ns)
                while pose is None and time.monotonic() < deadline and not self.stop.wait(.005):
                    pose = state.buffer.lookup(item.stamp_ns)
                if pose is None:
                    state.diagnostics = {"state": "waiting_for_vio"}
                    continue
                images = [gray(m) for m in item.images]
                O = matrix_from_pose(pose)
                metadata = {"stamp_ns": item.stamp_ns, "odom_from_imu": O.tolist()}
                metadata["tracking_epoch"] = item.tracking_epoch
                if item.view == self.head:
                    arrays = stereo_arrays(*images, self.features.match(*images))
                    result = self.request(item, "map", images=images, arrays=arrays, **metadata)
                else:
                    features = self.features.extract(images[0])
                    mask_ratio = load_gripper_mask_height_ratio(self.runtime_config)
                    arrays = query_arrays(features.keypoints, features.descriptors, images[0].shape[0], mask_ratio)
                    result = self.request(item, "localize", images=images, arrays=arrays,
                                                  camera_id=self.names.index(item.view) + 1, **metadata)
                    if result is not None:
                        result["gripper_mask_height_ratio"] = mask_ratio
                if not self.commit_result(item, result, O):
                    continue
                if item.view == self.head and state.clock.ready and time.monotonic() - self.last_geometry >= 1.:
                    geometry = self.request(item, "geometry")
                    with self.lock:
                        if (geometry is not None and item.generation == self.generation
                                and item.tracking_epoch == state.tracking_epoch and state.clock.ready):
                            self.publish_geometry(item.images[0], geometry["arrays"])
                            self.last_geometry = time.monotonic()
            except queue.Empty:
                continue
            except OSError as exc:
                self.get_logger().error(f"Mapping transport failed: {exc}")
                self.stop.set()
                rclpy.try_shutdown()
            except Exception as exc:
                with self.lock:
                    self.failed = True
                    for camera in self.cameras.values():
                        camera.correction = None
                    self.stats = {**self.stats, "state": "error", "error": str(exc)}
                self.get_logger().error(str(exc), throttle_duration_sec=5)

    def publish_status(self):
        with self.lock:
            now = self.get_clock().now().nanoseconds
            for name, state in self.cameras.items():
                payload = self.stats if name == self.head else state.diagnostics
                localized = (state.correction is not None and state.clock.ready
                             and state.tracking in {"TRACKING", "TRACKING_STATIC"}
                             and 0 <= now - state.last_vio_received_ns <= 2_000_000_000)
                payload = {**payload, "algorithm": "LooperVIO", "localized": localized,
                           "camera": name, "tracking_status": state.tracking, "generation": self.generation,
                           "session_id": self.session_id, "last_reset_wall_ns": self.last_reset_wall_ns,
                           "last_map_source_ns": self.last_map_source_ns}
                payload["map_revision"] = state.map_revision
                payload["source_clock_offset_ns"] = state.clock.offset_ns
                payload["clock_ready"] = state.clock.ready
                payload["clock_method"] = "initial_receive_delay_lower_envelope"
                if name != self.head:
                    age = time.monotonic() - state.last_match if state.last_match else None
                    payload["match_age_sec"] = age
                    payload["tracking_mode"] = ("unlocalized" if not localized else
                        "map_match" if payload.get("geometric_match") and age is not None and age < 1. else "vio_only")
                self.status_publishers[name].publish(String(data=json.dumps(payload)))

    def destroy_node(self):
        self.stop.set()
        self.work.put_nowait(None)
        self.worker.join(timeout=30)
        self.backend.close()
        return super().destroy_node()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config/cameras.json"))
    parser.add_argument("--runtime-config", type=Path, default=runtime_config_path())
    parser.add_argument("--socket", type=Path, default=Path("/run/looper/mapping.sock"))
    parser.add_argument("--feature-socket", type=Path, default=Path("/run/superglue/matcher.sock"))
    args = parser.parse_args()
    os.environ["ROS_DOMAIN_ID"] = str(json.loads(args.config.read_text())["ros_domain_id"])
    rclpy.init()
    node = LooperNode(args)
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
