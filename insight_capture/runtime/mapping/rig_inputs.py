"""ROS adapters for independently calibrated overlapping stereo views."""

from dataclasses import dataclass, replace
import threading

import numpy as np

from .geometry import StereoCalibration, matrix_from_transform, left_to_stereo_center
from .rig import LatestViewQueue, ViewPair
from .synchronization import StereoPairSynchronizer


@dataclass(frozen=True)
class ViewGeometry:
    calibration: StereoCalibration
    imu_to_left: np.ndarray
    imu_to_center: np.ndarray
    left_frame: str
    right_frame: str
    revision: int = 0


class RigInputs:
    def __init__(self, node, views):
        from sensor_msgs.msg import CameraInfo, Image
        from rclpy.qos import qos_profile_sensor_data

        self.node = node
        self.views = views
        self.lock = threading.Lock()
        self.infos = {v["name"]: [None, None] for v in views}
        self.geometry = {}
        self.revision = 0
        self.fingerprint = None
        self.last_keyframes = {}
        self.processed = {v["name"]: 0 for v in views}
        self.keyframes = {v["name"]: 0 for v in views}
        self.metrics = {}
        self.references = {}
        self.last_reference_publish = 0.0
        self.reference_dirty = set()
        self.reference_publishers = {}
        self.queue = LatestViewQueue([v["name"] for v in views])
        self.syncs = {v["name"]: StereoPairSynchronizer(
            tolerance_ns=int(node._args.stereo_tolerance_ms * 1e6)) for v in views}
        for view in views:
            name = view["name"]
            for eye, side in enumerate(("left", "right")):
                node.create_subscription(CameraInfo, view[side + "_info_topic"],
                    lambda message, n=name, i=eye: self.on_info(n, i, message), qos_profile_sensor_data)
                node.create_subscription(Image, view[side + "_image_topic"],
                    lambda message, n=name, i=eye: self.on_image(n, i, message), qos_profile_sensor_data)
        for name in [v["name"] for v in views[1:]] + ["front_right"]:
            from sensor_msgs.msg import PointCloud2
            from rclpy.qos import QoSProfile, DurabilityPolicy, ReliabilityPolicy
            qos = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                             reliability=ReliabilityPolicy.RELIABLE)
            base = "/insight9_sparse_map/calibration_keyframe/" + name
            self.reference_publishers[name] = (
                node.create_publisher(Image, base + "/image", qos),
                node.create_publisher(PointCloud2, base + "/points", qos))

    def on_info(self, name, eye, message):
        with self.lock:
            self.infos[name][eye] = message

    def on_image(self, name, eye, message):
        stamp = int(message.header.stamp.sec) * 1_000_000_000 + int(message.header.stamp.nanosec)
        pair = (self.syncs[name].push_left if eye == 0 else self.syncs[name].push_right)(stamp, message)
        if pair is not None:
            self.queue.put_nowait(ViewPair(pair.left_stamp_ns, pair.left,
                pair.right_stamp_ns, pair.right, name))

    def refresh(self):
        from rclpy.time import Time
        from rclpy.duration import Duration
        from std_srvs.srv import Empty

        with self.lock:
            infos = {name: tuple(value) for name, value in self.infos.items()}
        if any(any(info is None for info in value) for value in infos.values()):
            return
        geometry = {}
        try:
            identities = set()
            for view in self.views:
                for eye, side in enumerate(("left", "right")):
                    frame = infos[view["name"]][eye].header.frame_id
                    prefix = view[side + "_frame"] + "_"
                    if not frame.startswith(prefix) or frame == prefix:
                        raise ValueError("Rig inputs require versioned calibration frames")
                    identities.add(frame[len(prefix):])
            if len(identities) != 1:
                raise ValueError("Rig inputs contain mixed calibration versions")
            def lookup(target, source):
                value = self.node._tf_buffer.lookup_transform(target, source, Time(),
                    timeout=Duration(seconds=.02)).transform
                return matrix_from_transform((value.translation.x, value.translation.y, value.translation.z),
                    (value.rotation.x, value.rotation.y, value.rotation.z, value.rotation.w))
            for name, (left, right) in infos.items():
                if (left.width, left.height) != (right.width, right.height):
                    raise ValueError("Rig eye dimensions differ")
                calibration = StereoCalibration.from_camera_info(left.p, right.p, left.width, left.height)
                imu_to_left = lookup(self.node._args.imu_frame, left.header.frame_id)
                left_to_right = lookup(left.header.frame_id, right.header.frame_id)
                if not np.isclose(calibration.baseline_m, np.linalg.norm(left_to_right[:3, 3]), rtol=.01, atol=1e-5):
                    raise ValueError("Rig TF baseline and projection matrix disagree")
                geometry[name] = ViewGeometry(calibration, imu_to_left,
                    imu_to_left @ left_to_stereo_center(left_to_right), left.header.frame_id, right.header.frame_id)
        except Exception as exc:
            self.node.get_logger().warning(f"Waiting for complete rig geometry: {exc}", throttle_duration_sec=5)
            with self.lock:
                self.geometry = {}
            self.node._calibration = None
            self.node._imu_to_left = self.node._imu_to_center = None
            return
        fingerprint = tuple((name, value.left_frame, value.right_frame,
            value.calibration.left_projection.tobytes(), value.calibration.right_projection.tobytes(),
            np.round(value.imu_to_left, 9).tobytes(), np.round(value.imu_to_center, 9).tobytes())
            for name, value in geometry.items())
        with self.lock:
            changed = self.fingerprint is not None and self.fingerprint != fingerprint
            if changed:
                self.geometry = {}
        if changed:
            self.node._on_reset(Empty.Request(), Empty.Response())
            self.node.get_logger().warning("Rig calibration changed; old map and references invalidated")
        with self.lock:
            if self.fingerprint != fingerprint:
                self.revision += 1
            self.geometry = {name: replace(value, revision=self.revision) for name, value in geometry.items()}
            self.fingerprint = fingerprint
            primary = geometry[self.views[0]["name"]]
        self.node._calibration = primary.calibration
        self.node._imu_to_left = primary.imu_to_left
        self.node._imu_to_center = primary.imu_to_center

    def snapshot(self, pair):
        with self.lock:
            geometry = self.geometry.get(pair.view)
            if geometry is None or pair.left.header.frame_id != geometry.left_frame or pair.right.header.frame_id != geometry.right_frame:
                return None
            self.processed[pair.view] += 1
            return geometry

    def reset(self):
        self.queue.clear()
        with self.lock:
            self.last_keyframes.clear()
            self.references.clear()
            self.keyframes = {name: 0 for name in self.keyframes}
            self.metrics.clear()
            self.reference_dirty.update(self.reference_publishers)

    def status(self):
        with self.lock:
            return {"revision": self.revision, "views": {v["name"]: {
                "ready": v["name"] in self.geometry,
                "attempts": self.processed[v["name"]],
                "keyframes": self.keyframes[v["name"]],
                "baseline_m": (self.geometry[v["name"]].calibration.baseline_m
                               if v["name"] in self.geometry else None),
                **self.metrics.get(v["name"], {})}
                for v in self.views}}
