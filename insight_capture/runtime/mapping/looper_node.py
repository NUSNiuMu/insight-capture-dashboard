"""ROS transport for the serialized LooperVIO map and independent wrist VIOs."""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass, field
from functools import partial
import json
import os
from pathlib import Path
import queue
import threading
import time
import traceback
import uuid
from types import SimpleNamespace

import cv2
import numpy as np
from scipy.spatial.transform import Rotation
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data, QoSProfile, DurabilityPolicy, ReliabilityPolicy
from rclpy.time import Time
from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped, TransformStamped
from sensor_msgs.msg import CameraInfo, CompressedImage, Image, Imu, PointCloud2, PointField
from std_msgs.msg import String, Header
from std_srvs.srv import Empty
from tf2_msgs.msg import TFMessage
from tf2_ros import Buffer, TransformBroadcaster, StaticTransformBroadcaster, TransformException

from .geometry import PoseSample, matrix_from_pose, matrix_from_transform
from .looper_backend import LooperBackend
from .looper_features import stereo_arrays, query_arrays
from .mono_wrist import wrist_rectification
from .pose_correction_filter import PoseCorrectionFilter
from .frame_odometry import FrameOdometry
from .superglue_backend import IpcSuperGlueBackend, InferenceUnavailable
from insight_capture.runtime.diagnostics import DiagnosticEvents
from insight_capture.runtime.ros_inputs import CameraInputs
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
    mono: bool = False
    mono_maps: tuple | None = None
    last_mono_frame: float = 0.
    buffer: PoseBuffer = field(default_factory=PoseBuffer)
    odometry: FrameOdometry = field(default_factory=FrameOdometry)
    fusion: PoseCorrectionFilter = field(default_factory=PoseCorrectionFilter)
    correction: object = None
    stationary_correction: np.ndarray | None = None
    resume_after_ns: int | None = None
    info: object = None
    center: object = None
    geometry_id: object = None
    next_image: int = 0
    clock: SourceClock = field(default_factory=SourceClock)
    latest_source_ns: int = 0
    reset_source_ns: int = 0
    last_vio_received_ns: int = 0
    last_raw_source_ns: int = 0
    last_raw_received_ns: int = 0
    tracking: str = "UNKNOWN"
    diagnostics: dict = field(default_factory=dict)
    last_match: float = 0.
    map_revision: int = 0
    tracking_epoch: int = 0
    last_pose_reset_wall_ns: int = 0
    last_pose_tick: int | None = None


class LooperNode(Node):
    def __init__(self, args):
        super().__init__("insight_looper_mapping")
        self.events = DiagnosticEvents(self.get_logger())
        config = json.loads(args.config.read_text())
        self.runtime_config = args.runtime_config
        load_gripper_mask_height_ratio(self.runtime_config)
        self.head = next(c["name"] for c in config["cameras"] if c.get("teleop_role") == "head")
        wrists = {c["name"]: c for c in config["cameras"] if c.get("enabled", True)
                  and c.get("teleop_role") in {"right_hand", "left_hand"}
                  and (c.get("dashboard_pose_stream") or c.get("local_uvc"))}
        self.names = (self.head, *wrists)
        self.inputs = {name: {
            "left_info_topic": f"/{name}/camera/infra1/camera_info",
            "left_image_topic": f"/{name}/camera/infra1/image_rect_raw",
            "vio_topic": f"/{name}/camera/vio_100hz",
            "imu_frame": name + "_camera_imu",
            "center_left_frame": name + "_camera_left",
            "center_right_frame": name + "_camera_right",
        } for name in self.names[1:]}
        self.inputs[self.head] = config["mapping"]
        self.cameras = {name: CameraState(mono=bool(wrists.get(name, {}).get("local_uvc")))
                        for name in self.names}
        self.frames = {name: name + "_global_camera_center" for name in self.names}
        self.frames[self.head] = self.inputs[self.head]["mapping_camera_frame"]
        self.backend = LooperBackend(args.socket)
        self.features = IpcSuperGlueBackend(Path("/run/superglue/matcher.sock"))
        self.lock = threading.RLock()
        self.processing = threading.Lock()
        self.work = LatestViewQueue((*self.names[1:], self.head))
        self.prepared = LatestViewQueue((*self.names[1:], self.head))
        self.stop = threading.Event()
        self.sync = StereoPairSynchronizer(tolerance_ns=2_000_000)
        self.tf_buffer = Buffer()
        self.tf_broadcaster = TransformBroadcaster(self)
        self.static_tf = StaticTransformBroadcaster(self)
        self.ready = False
        self.generation = 0
        self.session_id = uuid.uuid4().hex
        self.last_reset_wall_ns = self.get_clock().now().nanoseconds
        self.last_map_source_ns = 0
        self.head_raw_correction = None
        self.stats = {"state": "waiting_for_inputs", "map_point_count": 0}
        self.right_info = None
        self.pose_publishers = {}
        self.status_publishers = {}
        geometry_qos = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                                 reliability=ReliabilityPolicy.RELIABLE)
        self.camera_inputs = CameraInputs(self)
        self.camera_inputs.create_subscription(TFMessage, "/tf_static", self.on_static_tf,
                                 QoSProfile(depth=100, durability=DurabilityPolicy.TRANSIENT_LOCAL))
        self.feature_publisher = self.create_publisher(PointCloud2, "/insight9_sparse_map/features", geometry_qos)
        base = "/insight9_sparse_map/calibration_keyframe"
        self.reference_image = self.create_publisher(Image, base + "/image", geometry_qos)
        self.reference_points = self.create_publisher(PointCloud2, base + "/points", geometry_qos)
        self.last_geometry = 0.
        self.last_pose_tick = None
        self.pose_corrections = deque(maxlen=1000)
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
            if self.cameras[name].mono:
                source = f"/{name}/map_query"
                self.camera_inputs.create_subscription(CameraInfo, source + "/camera_info",
                    partial(self.on_mono_info, name), geometry_qos)
                self.camera_inputs.create_subscription(CompressedImage, source + "/image/compressed",
                    partial(self.on_mono_image, name),
                    QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT))
                continue
            inputs = self.inputs[name]
            self.camera_inputs.create_subscription(CameraInfo, inputs["left_info_topic"],
                                     lambda m, n=name: setattr(self.cameras[n], "info", m), qos_profile_sensor_data)
            self.camera_inputs.create_subscription(PoseStamped, inputs["vio_topic"],
                                     lambda m, n=name: self.on_vio(n, m), qos_profile_sensor_data)
            self.camera_inputs.create_subscription(Imu, f"/{name}/camera/imu",
                                     lambda m, n=name: self.on_imu(n, m), qos_profile_sensor_data)
            if name == self.head:
                self.camera_inputs.create_subscription(PoseStamped, f"/{name}/camera/vio_image",
                    lambda m, n=name: self.on_frame_pose(n, m.header, m.pose), qos_profile_sensor_data)
            else:
                self.camera_inputs.create_subscription(PoseWithCovarianceStamped, f"/{name}/camera/vio_image_cov",
                    lambda m, n=name: self.on_frame_pose(n, m.header, m.pose.pose), qos_profile_sensor_data)
            self.camera_inputs.create_subscription(String, f"/{name}/camera/vio_status",
                                     lambda m, n=name: self.on_tracking(n, m.data), 1)
            self.camera_inputs.create_subscription(Image, inputs["left_image_topic"],
                                     lambda m, n=name: self.on_image(n, 0, m), qos_profile_sensor_data)
        self.camera_inputs.create_subscription(CameraInfo, self.inputs[self.head]["right_info_topic"],
                                 lambda m: setattr(self, "right_info", m), qos_profile_sensor_data)
        self.camera_inputs.create_subscription(Image, self.inputs[self.head]["right_image_topic"],
                                 lambda m: self.on_image(self.head, 1, m), qos_profile_sensor_data)
        self.create_service(Empty, "/insight_global/reset", self.reset_map)
        self.create_timer(.5, self.publish_status)
        self.create_timer(1. / 30., self.publish_poses)
        self.extractor = threading.Thread(target=self.run, args=(self.work, self.prepare),
                                          name="looper-features", daemon=True)
        self.worker = threading.Thread(target=self.run, args=(self.prepared, self.process),
                                       name="looper-owner", daemon=True)
        self.extractor.start()
        self.worker.start()
        self.camera_inputs.start()

    def lookup(self, target, source):
        t = self.tf_buffer.lookup_transform(target, source, Time()).transform
        return matrix_from_transform((t.translation.x, t.translation.y, t.translation.z),
                                     (t.rotation.x, t.rotation.y, t.rotation.z, t.rotation.w))

    def on_static_tf(self, message):
        with self.lock:
            for tf in message.transforms:
                self.tf_buffer.set_transform_static(tf, "camera_inputs")

    def configure(self, name):
        state = self.cameras[name]
        inputs = self.inputs[name]
        infos = [state.info, self.right_info] if name == self.head else [state.info]
        eyes = ("left", "right") if name == self.head else ("left",)
        missing = [f"{name}/{eye}/CameraInfo" for eye, info in zip(eyes, infos)
                   if info is None or not info.header.frame_id or info.width <= 0 or info.height <= 0
                   or not np.isfinite([*info.p, *info.r]).all() or info.p[0] <= 0 or info.p[5] <= 0]
        if missing:
            self.wait_for_inputs(name, missing)
            return False
        imu = inputs["imu_frame"]
        required = [(imu, frame) for frame in (
            *(info.header.frame_id for info in infos),
            inputs["center_left_frame"], inputs["center_right_frame"])]
        missing = [f"TF:{target}<-{source}" for target, source in dict.fromkeys(required)
                   if not self.tf_buffer.can_transform(target, source, Time())]
        if missing:
            self.wait_for_inputs(name, missing)
            return False
        if name != self.head and not self.ready:
            self.wait_for_inputs(name, [self.head + "/geometry"])
            return False
        models = []
        for i, info in enumerate(infos):
            extrinsic = self.lookup(imu, infos[0].header.frame_id)
            rectified = np.eye(4)
            rectified[:3, :3] = np.asarray(infos[0].r).reshape(3, 3).T
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
        identity = tuple((model["width"], model["height"], *np.round(model["intrinsics"], 10),
                          *np.round(model["imu_from_camera"], 10).ravel()) for model in models)
        identity += (tuple(np.round(center, 10).ravel()),)
        if identity != state.geometry_id:
            with self.processing, self.lock:
                if name == self.head:
                    self.clear()
                    self.backend.request("initialize", cameras=models)
                    for wrist in self.names[1:]:
                        self.cameras[wrist].geometry_id = None
                        self.cameras[wrist].center = None
                    self.head_image_size = (models[0]["width"], models[0]["height"])
                    self.ready = True
                else:
                    if state.geometry_id is not None:
                        state.tracking_epoch += 1
                        state.last_pose_reset_wall_ns = self.get_clock().now().nanoseconds
                        state.correction = None
                        state.stationary_correction = None
                        state.resume_after_ns = None
                        state.fusion = PoseCorrectionFilter()
                        state.last_match = 0.
                    self.backend.request("camera", camera_id=self.names.index(name) + 1, camera=models[0])
                state.center = center
                state.geometry_id = identity
            self.events.changed("geometry", name, "ready", "CameraInfo and required TF configured")
        return True

    def wait_for_inputs(self, name, missing):
        with self.lock:
            diagnostics = {"state": "waiting_for_inputs", "missing_inputs": missing}
            self.cameras[name].diagnostics = diagnostics
            if name == self.head:
                self.stats = {**self.stats, **diagnostics}
        self.events.changed("geometry", name, "waiting_for_inputs", "; ".join(missing), level="warning")

    def clear(self):
        self.generation += 1
        self.last_reset_wall_ns = self.get_clock().now().nanoseconds
        self.last_map_source_ns = 0
        self.head_raw_correction = None
        self.work.clear()
        self.prepared.clear()
        self.sync.clear()
        self.last_geometry = 0.
        self.last_pose_tick = None
        self.pose_corrections.clear()
        for state in self.cameras.values():
            state.reset_source_ns = state.latest_source_ns
            state.last_pose_reset_wall_ns = self.last_reset_wall_ns
            state.correction = None
            state.stationary_correction = None
            state.resume_after_ns = None
            state.fusion = PoseCorrectionFilter()
            state.next_image = 0
            state.diagnostics = {}
            state.last_match = 0.
            state.map_revision = 0
            state.last_pose_tick = None
        self.stats = {"state": "building", "map_point_count": 0}
        self.publish_geometry(None, [np.empty((0, 259), np.float32), np.empty((0, 5), np.float32)])

    def reset_map(self, request, response):
        with self.processing, self.lock:
            if self.ready:
                self.backend.request("reset")
            self.clear()
        return response

    def on_mono_info(self, name, info):
        with self.lock:
            state = self.cameras[name]
            if state.info is not None and state.info.header.frame_id == info.header.frame_id:
                return
            state.info = info
            state.tracking_epoch += 1
            state.last_pose_reset_wall_ns = max(self.last_reset_wall_ns, stamp_ns(info.header.stamp))
            state.geometry_id = None
            state.last_match = state.last_mono_frame = 0.
            state.diagnostics = {}

    def on_mono_image(self, name, image):
        stamp = stamp_ns(image.header.stamp)
        with self.lock:
            state = self.cameras[name]
            if (state.info is None or image.header.frame_id != state.info.header.frame_id
                    or stamp < max(self.last_reset_wall_ns, state.last_pose_reset_wall_ns)
                    or not 0 <= self.get_clock().now().nanoseconds - stamp <= 2_000_000_000):
                return
            received = time.monotonic()
            state.last_mono_frame = received
            self.work.put_nowait(SimpleNamespace(view=name, stamp_ns=stamp, images=(image,),
                generation=self.generation, tracking_epoch=state.tracking_epoch, received=received))

    def on_image(self, name, eye, message):
        state = self.cameras[name]
        if state.tracking not in {"TRACKING", "TRACKING_STATIC"} or not state.clock.ready:
            return
        stamp = stamp_ns(message.header.stamp)
        if name == self.head:
            pair = (self.sync.push_left if eye == 0 else self.sync.push_right)(stamp, message)
            if pair is None:
                return
            stamp = pair.stamp_ns
            item = SimpleNamespace(view=name, stamp_ns=stamp, images=(pair.left, pair.right))
            target_hz = 2.
        else:
            item = SimpleNamespace(view=name, stamp_ns=stamp, images=(message,))
            target_hz = 4.
        due, state.next_image = select_timestamp(stamp, state.next_image, target_hz)
        if due:
            item.generation = self.generation
            item.tracking_epoch = state.tracking_epoch
            self.work.put_nowait(item)

    def on_tracking(self, name, tracking):
        with self.lock:
            state = self.cameras[name]
            reset = (tracking in {"NOT_INITED", "NO_CALIBRATION", "RESTARTING", "STOPPED"}
                     and tracking != state.tracking
                     and (state.last_raw_received_ns or state.latest_source_ns
                          or state.correction is not None))
            if (name != self.head and tracking == "TRACKING" and state.tracking != tracking
                    and (state.tracking == "TRACKING_STATIC" or state.stationary_correction is not None)):
                state.resume_after_ns = state.latest_source_ns
                state.next_image = 0
                head_correction = self.cameras[self.head].correction
                if (state.stationary_correction is None and state.correction is not None
                        and head_correction is not None):
                    state.stationary_correction = head_correction @ state.correction
            state.tracking = tracking
        if reset:
            self.invalidate_camera(name)
        self.events.changed("sdk_tracking", name, tracking,
                            level="info" if tracking in {"TRACKING", "TRACKING_STATIC"} else "warning")

    def invalidate_camera(self, name):
        with self.lock:
            state = self.cameras[name]
            state.correction = None
            state.stationary_correction = None
            state.resume_after_ns = None
            state.fusion = PoseCorrectionFilter()
            state.tracking_epoch += 1
            state.last_pose_reset_wall_ns = self.get_clock().now().nanoseconds
            state.last_pose_tick = None
            state.buffer.clear()
            state.odometry = FrameOdometry()
            state.clock = SourceClock()
            state.latest_source_ns = state.last_vio_received_ns = 0
            state.reset_source_ns = 0
            state.next_image = 0
            state.last_raw_source_ns = state.last_raw_received_ns = 0
            state.last_match = 0.
            state.diagnostics = {"state": "waiting_for_vio", "geometric_match": False}
        if name == self.head:
            self.reset_map(None, Empty.Response())

    def on_vio(self, name, message):
        source = stamp_ns(message.header.stamp)
        state = self.cameras[name]
        received = self.get_clock().now().nanoseconds
        if state.last_raw_received_ns:
            source_delta = source - state.last_raw_source_ns
            received_delta = received - state.last_raw_received_ns
            if source_delta < 0:
                self.get_logger().warning(f"{name}: SDK source clock restarted; invalidating localization")
                self.invalidate_camera(name)
            elif abs(source_delta - received_delta) > 2_000_000_000:
                with self.lock:
                    state.clock = SourceClock()
                    state.buffer.clear()
                    state.odometry.reset_motion()
                    state.last_pose_tick = None
                    state.latest_source_ns = state.last_vio_received_ns = 0
                    state.next_image = 0
                    state.tracking_epoch += 1
                    state.last_pose_reset_wall_ns = received
                self.events.changed("source_clock", name, "resynchronizing",
                                    "Clock offset changed; retaining map and localization", level="warning")
            elif received_delta > 2_000_000_000:
                with self.lock:
                    state.odometry.reset_motion()
                self.events.changed("source_clock", name, "resumed",
                                    "Transport gap ended with continuous SDK time")
        with self.lock:
            clock_was_ready = state.clock.ready
            state.clock.observe(source, received)
            state.last_raw_source_ns = source
            state.last_raw_received_ns = received
        if not clock_was_ready and state.clock.ready:
            self.events.changed("source_clock", name, "ready", "Initial VIO clock samples collected")

    def on_imu(self, name, message):
        a = message.linear_acceleration
        with self.lock:
            self.cameras[name].odometry.add_imu(stamp_ns(message.header.stamp), [a.x, a.y, a.z])

    def on_frame_pose(self, name, header, pose):
        if self.cameras[name].tracking not in {"TRACKING", "TRACKING_STATIC"}:
            return
        p, q = pose.position, pose.orientation
        sample = PoseSample(stamp_ns(header.stamp), np.array([p.x, p.y, p.z]),
                            np.array([q.x, q.y, q.z, q.w]))
        with self.lock:
            state = self.cameras[name]
            sample = state.odometry.update(sample)
            if sample is None:
                return
            state.buffer.append(sample)
            state.latest_source_ns = sample.stamp_ns
            state.last_vio_received_ns = self.get_clock().now().nanoseconds

    def publish_poses(self):
        with self.lock:
            head = self.cameras[self.head]
            if not self.ready or head.correction is None:
                return
            states = {name: state for name, state in self.cameras.items()
                      if state.correction is not None and state.clock.ready
                      and state.tracking in {"TRACKING", "TRACKING_STATIC"}}
            # Correction injection uses each device's latest native anchor.
            # Published poses below always use that device's timestamped VIO.
            anchors = {}
            for name, state in tuple(states.items()):
                pose = state.buffer.lookup(state.latest_source_ns)
                if pose is None:
                    del states[name]
                    continue
                anchors[name] = matrix_from_pose(pose) @ state.center
            if not states:
                return
            latest_tick = max(state.latest_source_ns + state.clock.offset_ns
                              for state in states.values()) * 30 // 1_000_000_000
            if self.last_pose_tick is not None and (
                    latest_tick < self.last_pose_tick
                    or latest_tick - self.last_pose_tick > self.pose_corrections.maxlen):
                # A wall-clock correction starts a new publication timeline.
                # Never freeze on future ticks or replay an unbounded time gap.
                self.pose_corrections.clear()
                self.last_pose_tick = None
                for state in self.cameras.values():
                    state.last_pose_tick = None
            first_tick = latest_tick if self.last_pose_tick is None else self.last_pose_tick + 1
            for tick in range(first_tick, latest_tick + 1):
                stamp = tick * 1_000_000_000 // 30
                refining = {name: state for name, state in states.items()
                            if state.stationary_correction is None}
                positions = {name: (state.correction @ anchors[name])[:3, 3]
                             for name, state in refining.items()}
                positions[self.head] = head.fusion.native[:3, 3]
                relative_step = 0.
                for name, state in refining.items():
                    if name == self.head:
                        continue
                    native = anchors[name]
                    previous = state.correction @ native
                    lever = max(np.linalg.norm(positions[name] - p) for p in positions.values())
                    state.correction = state.fusion.output(native, stamp, lever_arm=lever)
                    relative_step = max(relative_step, np.linalg.norm(
                        (state.correction @ native)[:3, 3] - previous[:3, 3]))
                if self.head in states:
                    lever = max(np.linalg.norm((state.correction @ anchors[name])[:3, 3]
                                              - anchors[self.head][:3, 3])
                                for name, state in refining.items())
                    head.correction = head.fusion.output(anchors[self.head], stamp,
                        speed=max(0., .15 - relative_step * 30.), lever_arm=lever)
                corrections = {}
                for name, state in states.items():
                    correction = head.correction
                    if name != self.head:
                        correction = state.stationary_correction
                        if correction is None:
                            correction = head.correction @ state.correction
                            native = anchors[name]
                            if (state.tracking == "TRACKING_STATIC" and state.resume_after_ns is None
                                    and np.allclose(state.fusion.posterior @ native, state.correction @ native,
                                                    atol=1e-4, rtol=0)):
                                state.stationary_correction = correction.copy()
                    corrections[name] = (state.tracking_epoch, correction.copy())
                # Late VIO samples use the correction recorded for their tick.
                self.pose_corrections.append((tick, corrections))
                self.last_pose_tick = tick
            transforms = []
            for name, state in states.items():
                latest = state.latest_source_ns + state.clock.offset_ns
                for tick, corrections in self.pose_corrections:
                    if state.last_pose_tick is not None and tick <= state.last_pose_tick:
                        continue
                    stamp = tick * 1_000_000_000 // 30
                    if stamp > latest:
                        break
                    correction = corrections.get(name)
                    if correction is None or correction[0] != state.tracking_epoch:
                        continue
                    pose = state.buffer.lookup(stamp - state.clock.offset_ns)
                    if pose is None:
                        continue
                    transform = correction[1] @ matrix_from_pose(pose) @ state.center
                    transforms.append(self.publish_camera_pose(name, stamp, transform))
                    state.last_pose_tick = tick
            if transforms:
                self.tf_broadcaster.sendTransform(transforms)

    def publish_camera_pose(self, name, stamp, transform):
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
        return tf

    def request(self, item, command, **metadata):
        with self.processing:
            if (item.generation != self.generation
                    or item.tracking_epoch != self.cameras[item.view].tracking_epoch
                    or item.stamp_ns < self.cameras[item.view].reset_source_ns):
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

    def update_correction(self, state, correction, source_ns, method):
        pose = state.buffer.lookup(state.latest_source_ns)
        if pose is None:
            state.fusion.suspend("waiting_for_vio")
            state.correction = None
            return False
        native = matrix_from_pose(pose) @ state.center
        if state is not self.cameras[self.head]:
            correction = np.linalg.solve(self.head_raw_correction, correction)
            if state.stationary_correction is not None:
                # Start smoothing from the map transform that is actually being published.
                head_correction = self.cameras[self.head].correction
                if head_correction is None:
                    return False
                seed = np.linalg.solve(head_correction, state.stationary_correction)
                state.fusion.emitted = state.fusion.posterior = seed
        accepted = method(correction, native, state.latest_source_ns + state.clock.offset_ns, source_ns)
        state.correction = state.fusion.emitted if state.fusion.valid else None
        return accepted

    def commit_result(self, item, result, O):
        state = self.cameras[item.view]
        with self.lock:
            if (result is None or item.generation != self.generation
                    or item.tracking_epoch != state.tracking_epoch):
                return False
            if item.view == self.head:
                if result["mapped"]:
                    self.head_raw_correction = np.asarray(result["map_from_imu"]) @ np.linalg.inv(O)
                    if result["verified_relocalization"] or state.fusion.posterior is None:
                        self.update_correction(state, self.head_raw_correction, item.stamp_ns,
                                               partial(state.fusion.observe, verified=result["verified_relocalization"]))
                elif not result["landmarks"]:
                    state.correction = None
                    state.fusion.suspend("map_not_ready")
                state.map_revision = result["map_revision"]
                self.last_map_source_ns = item.stamp_ns
                for update in result["wrist_updates"]:
                    wrist = self.cameras[self.names[int(update["camera_id"]) - 1]]
                    if update["tracking_epoch"] != wrist.tracking_epoch:
                        continue
                    if wrist.mono:
                        if wrist.diagnostics.get("confirmed"):
                            wrist.map_revision = update["map_revision"]
                            if not update["localized"]:
                                wrist.diagnostics.update(state="searching", geometric_match=False,
                                                         confirmed=False, reason=update["reason"])
                        continue
                    wrist.map_revision = update["map_revision"]
                    if (wrist.resume_after_ns is not None or wrist.stationary_correction is not None
                            or wrist.tracking == "TRACKING_STATIC"):
                        continue
                    if update["localized"]:
                        accepted = self.update_correction(
                            wrist, np.asarray(update["map_from_imu"]) @ np.linalg.inv(update["odom_from_imu"]),
                            update["source_ns"], wrist.fusion.revise)
                    else:
                        wrist.fusion.reject(update["reason"])
                        accepted = False
                    wrist.diagnostics.update(
                        state="tracking" if accepted else "predicting" if wrist.correction is not None else "searching",
                        geometric_match=bool(update["localized"]), confirmed=accepted,
                        reason=update["reason"] if not update["localized"] else wrist.fusion.diagnostics["mode"])
                    wrist.diagnostics["map_update_alignment"] = update["alignment"]
                self.stats = {"state": "mapping" if result["localization_ready"] else "building", "map_point_count": result["landmarks"],
                              "geometric_match": result["verified_relocalization"],
                              "keyframes": result["keyframes"], "raw_landmarks": result["raw_landmarks"]}
            else:
                if not self.wrist_match_allowed(state, item.stamp_ns):
                    return False
                M = np.asarray(result["map_from_imu"]) if result["localized"] else None
                confirmed = (M is not None and self.update_correction(
                    state, M @ np.linalg.inv(O), item.stamp_ns,
                    partial(state.fusion.observe, verified=result["verified_relocalization"])))
                if confirmed:
                    state.stationary_correction = None
                    state.resume_after_ns = None
                    state.last_match = time.monotonic()
                state.map_revision = result["map_revision"]
                state.diagnostics = {"state": "tracking" if state.correction is not None else "searching",
                                     "geometric_match": result["localized"], "confirmed": bool(confirmed),
                                     "reason": result.get("reason"), **{key: result[key] for key in
                                      ("matches", "inliers", "grid_cells", "reprojection_rms_px", "gripper_mask_height_ratio") if key in result}}
            return item.view == self.head or confirmed

    @staticmethod
    def wrist_match_allowed(state, stamp):
        if state.resume_after_ns is not None:
            return state.tracking in {"TRACKING", "TRACKING_STATIC"} and stamp > state.resume_after_ns
        return state.tracking == "TRACKING" or (
            state.tracking == "TRACKING_STATIC" and state.correction is None)

    def prepare(self, item):
        state = self.cameras[item.view]
        if state.mono:
            self.prepare_mono(item, state)
            return
        try:
            configured = self.configure(item.view)
        except TransformException as exc:
            self.wait_for_inputs(item.view, [str(exc)])
            return
        if (not configured or item.generation != self.generation
                or item.tracking_epoch != self.cameras[item.view].tracking_epoch):
            return
        if item.view != self.head and not self.wrist_match_allowed(state, item.stamp_ns):
            return
        infos = (state.info, self.right_info)
        if any(image.header.frame_id != info.header.frame_id
               for image, info in zip(item.images, infos)):
            state.diagnostics = {"state": "waiting_for_inputs", "missing_inputs": [
                f"frame_id:{image.header.frame_id}!={info.header.frame_id}"
                for image, info in zip(item.images, infos) if image.header.frame_id != info.header.frame_id]}
            if item.view == self.head:
                self.wait_for_inputs(item.view, state.diagnostics["missing_inputs"])
            return
        deadline = time.monotonic() + .1
        pose = state.buffer.lookup(item.stamp_ns)
        while pose is None and time.monotonic() < deadline and not self.stop.wait(.005):
            pose = state.buffer.lookup(item.stamp_ns)
        if pose is None:
            state.diagnostics = {"state": "waiting_for_frame_pose", "missing_inputs": [
                f"{item.view}/vio_image"], "image_stamp_ns": item.stamp_ns}
            if item.view == self.head:
                self.stats = {**self.stats, **state.diagnostics}
            return
        item.gray = [gray(m) for m in item.images]
        item.odom = matrix_from_pose(pose)
        if item.view == self.head:
            item.arrays = stereo_arrays(*item.gray, self.features.match(*item.gray))
        else:
            features = self.features.extract(item.gray[0])
            item.mask_ratio = load_gripper_mask_height_ratio(self.runtime_config)
            item.arrays = query_arrays(features.keypoints, features.descriptors,
                                       item.gray[0].shape[0], item.mask_ratio)
        self.events.changed("inference", item.view, "available")
        self.prepared.put_nowait(item)

    def prepare_mono(self, item, state):
        with self.lock:
            if item.generation != self.generation or item.tracking_epoch != state.tracking_epoch:
                return
            if not self.ready or self.stats["state"] != "mapping":
                state.diagnostics = {"state": "searching", "reason": "map_not_ready"}
                return
            info = state.info
            identity = (info.header.frame_id, self.head_image_size)
            configured = state.geometry_id == identity
        if not configured:
            rotation = np.asarray(info.r).reshape(3, 3)
            model, maps, center = wrist_rectification(info.k, info.d, rotation, identity[1])
            with self.processing, self.lock:
                if item.generation != self.generation or item.tracking_epoch != state.tracking_epoch:
                    return
                self.backend.request("camera", camera_id=self.names.index(item.view) + 1, camera=model)
                state.geometry_id, state.mono_maps, state.center = identity, maps, center
        try:
            image = cv2.imdecode(np.frombuffer(item.images[0].data, np.uint8), cv2.IMREAD_GRAYSCALE)
        except cv2.error:
            image = None
        if image is None or image.shape != (info.height, info.width):
            with self.lock:
                if item.generation == self.generation and item.tracking_epoch == state.tracking_epoch:
                    state.diagnostics = {"state": "waiting_for_inputs", "reason": "uvc_image_calibration_mismatch"}
            return
        rectified = cv2.remap(image, *state.mono_maps, cv2.INTER_LINEAR)
        features = self.features.extract(rectified)
        item.gray = [rectified]
        item.arrays = (features.keypoints, features.descriptors)
        self.events.changed("inference", item.view, "available")
        self.prepared.put_nowait(item)

    def process_mono(self, item, state):
        if time.monotonic() - item.received >= 1.:
            return
        camera_id = self.names.index(item.view) + 1
        result = self.request(item, "localize", images=item.gray, arrays=item.arrays,
                              camera_id=camera_id, stamp_ns=item.stamp_ns, tracking_epoch=item.tracking_epoch,
                              odom_from_imu=np.eye(4).tolist())
        with self.lock:
            if (result is None or item.generation != self.generation or item.tracking_epoch != state.tracking_epoch
                    or time.monotonic() - item.received >= 1.):
                return
            head_correction = self.cameras[self.head].correction
            geometric_match = result["localized"]
            verified = geometric_match and result["verified_relocalization"]
            confirmed = verified and head_correction is not None and self.head_raw_correction is not None
            reason = result.get("reason") if not geometric_match else (
                "unverified_relocalization" if not verified else None if confirmed else "map_not_ready")
            state.map_revision = result["map_revision"]
            state.diagnostics = {"state": "tracking" if confirmed else "searching", "confirmed": confirmed,
                                 "geometric_match": geometric_match, "reason": reason,
                                 **{key: result[key] for key in ("matches", "inliers", "grid_cells",
                                    "reprojection_rms_px") if key in result}}
            if not confirmed:
                return
            pose = np.asarray(result["map_from_imu"]) @ state.center
            pose = head_correction @ np.linalg.solve(self.head_raw_correction, pose)
            tf = self.publish_camera_pose(item.view, item.stamp_ns, pose)
            self.tf_broadcaster.sendTransform(tf)
            state.last_match = item.received
        self.request(item, "accept", camera_id=camera_id, stamp_ns=item.stamp_ns,
                     tracking_epoch=item.tracking_epoch)

    def process(self, item):
        state = self.cameras[item.view]
        if state.mono:
            self.process_mono(item, state)
            return
        metadata = {"stamp_ns": item.stamp_ns, "odom_from_imu": item.odom.tolist(),
                    "tracking_epoch": item.tracking_epoch}
        if item.view == self.head:
            result = self.request(item, "map", images=item.gray, arrays=item.arrays, **metadata)
        else:
            if not self.wrist_match_allowed(state, item.stamp_ns):
                return
            result = self.request(item, "localize", images=item.gray, arrays=item.arrays,
                                  camera_id=self.names.index(item.view) + 1, **metadata)
            if result is not None:
                result["gripper_mask_height_ratio"] = item.mask_ratio
        committed = self.commit_result(item, result, item.odom)
        if result is not None and item.view != self.head and committed:
            self.request(item, "accept", camera_id=self.names.index(item.view)+1,
                         stamp_ns=item.stamp_ns, tracking_epoch=item.tracking_epoch)
        if not committed:
            return
        if item.view == self.head and result["mapped"] and state.clock.ready and time.monotonic() - self.last_geometry >= 1.:
            geometry = self.request(item, "geometry")
            with self.lock:
                if (geometry is not None and item.generation == self.generation
                        and item.tracking_epoch == state.tracking_epoch and state.clock.ready):
                    self.publish_geometry(item.images[0], geometry["arrays"])
                    self.last_geometry = time.monotonic()

    def run(self, pending, callback):
        while not self.stop.is_set():
            try:
                item = pending.get(timeout=.25)
                if item is None:
                    break
                if item.generation != self.generation:
                    continue
                callback(item)
            except queue.Empty:
                continue
            except InferenceUnavailable as exc:
                with self.lock:
                    state = self.cameras[item.view]
                    state.diagnostics = {"state": "waiting_for_inference", "error": str(exc)}
                    if not state.mono:
                        self.stats = {**self.stats, **state.diagnostics}
                self.events.changed("inference", item.view, "unavailable", str(exc), level="warning")
            except Exception as exc:
                with self.lock:
                    self.stats = {**self.stats, "state": "error", "error": str(exc)}
                self.get_logger().error(f"Localization stopped: {type(exc).__name__}: {exc}\n"
                                        + traceback.format_exc())
                self.publish_status()
                self.stop.set()
                rclpy.try_shutdown()

    def publish_status(self):
        with self.lock:
            now = self.get_clock().now().nanoseconds
            for name, state in self.cameras.items():
                payload = self.stats if name == self.head else state.diagnostics
                if state.mono:
                    self.publish_mono_status(name, state)
                    continue
                localized = (state.correction is not None and state.clock.ready
                             and state.tracking in {"TRACKING", "TRACKING_STATIC"}
                             and 0 <= now - state.last_vio_received_ns <= 2_000_000_000)
                payload = {**payload, "algorithm": "LooperVIO", "localized": localized,
                           "fusion": state.fusion.diagnostics,
                           "odometry": state.odometry.diagnostics,
                           "camera": name, "tracking_status": state.tracking, "generation": self.generation,
                           "tracking_epoch": state.tracking_epoch,
                           "last_pose_reset_wall_ns": state.last_pose_reset_wall_ns,
                           "session_id": self.session_id, "last_reset_wall_ns": self.last_reset_wall_ns,
                           "last_map_source_ns": self.last_map_source_ns}
                payload["map_revision"] = state.map_revision
                payload["source_clock_offset_ns"] = state.clock.offset_ns
                payload["clock_ready"] = state.clock.ready
                payload["clock_method"] = "initial_receive_delay_minimum"
                if name != self.head:
                    age = time.monotonic() - state.last_match if state.last_match else None
                    payload["match_age_sec"] = age
                    payload["holding_map_correction"] = state.stationary_correction is not None
                    payload["waiting_for_fresh_match"] = state.resume_after_ns is not None
                    payload["tracking_mode"] = ("unlocalized" if not localized else
                        "map_match" if state.stationary_correction is None and payload.get("confirmed")
                            and age is not None and age < 1. else "vio_only")
                self.status_publishers[name].publish(String(data=json.dumps(payload)))

    def publish_mono_status(self, name, state):
        now = time.monotonic()
        recent = state.last_mono_frame > 0. and now - state.last_mono_frame < 1.
        age = now - state.last_match if state.last_match else None
        payload = state.diagnostics.copy()
        if state.info is not None and state.info.k[0] <= 0:
            payload = {"state": "waiting_for_inputs", "reason": "uvc_calibration_missing_or_invalid"}
        elif not recent:
            payload = {"state": "waiting_for_inputs", "reason": "uvc_image_unavailable"}
        localized = bool(recent and payload.get("confirmed") and age is not None and age < 1.)
        payload.update(camera=name, algorithm="LooperVIO", visual_only=True, localized=localized,
                       confirmed=localized, clock_ready=recent, clock_method="jetson_capture_stamp",
                       tracking_mode="map_match" if localized else "unlocalized", match_age_sec=age,
                       map_revision=state.map_revision, generation=self.generation, session_id=self.session_id,
                       tracking_epoch=state.tracking_epoch, last_pose_reset_wall_ns=state.last_pose_reset_wall_ns,
                       last_reset_wall_ns=self.last_reset_wall_ns, last_map_source_ns=self.last_map_source_ns)
        self.status_publishers[name].publish(String(data=json.dumps(payload)))

    def destroy_node(self):
        self.stop.set()
        self.camera_inputs.close()
        self.work.put_nowait(None)
        self.prepared.put_nowait(None)
        self.extractor.join(timeout=30)
        self.worker.join(timeout=30)
        self.backend.close()
        return super().destroy_node()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config/cameras.json"))
    parser.add_argument("--runtime-config", type=Path, default=runtime_config_path())
    parser.add_argument("--socket", type=Path, default=Path("/run/looper/mapping.sock"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    os.environ["ROS_DOMAIN_ID"] = str(config["ros_domain_id"])
    from insight_capture.runtime.ros.network import configure_ros_network
    configure_ros_network(config, include_relay_links=True)
    rclpy.init()
    node = LooperNode(args)
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
