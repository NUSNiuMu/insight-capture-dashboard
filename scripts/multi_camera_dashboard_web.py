#!/usr/bin/env python3

import argparse
import asyncio
import contextlib
import math
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Dict, List, Optional, Set, Tuple
from urllib.parse import quote

import numpy as np
from aiohttp import web

try:
    import rclpy
    from geometry_msgs.msg import PoseStamped
    from rclpy.callback_groups import ReentrantCallbackGroup
    from rclpy.executors import MultiThreadedExecutor
    from rclpy.node import Node
    from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
    from sensor_msgs.msg import CameraInfo
    from sensor_msgs.msg import CompressedImage, Image as RosImage
except Exception:  # pragma: no cover - fake mode can run without ROS imports
    rclpy = None
    PoseStamped = None
    ReentrantCallbackGroup = None
    MultiThreadedExecutor = None
    Node = object
    QoSProfile = None
    ReliabilityPolicy = None
    HistoryPolicy = None
    DurabilityPolicy = None
    CameraInfo = None
    CompressedImage = None
    RosImage = None

from camera_setup import IMAGE_STREAMS, build_dashboard_config, camera_info_topic, image_topic, load_setup
from live_alignment import LiveAlignmentMixin
from post_processing import PostProcessor, RecordingManager, RosbagStore, build_default_topics, load_json_config
from session_alignment import PoseSample


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


@dataclass
class PoseSpec:
    name: str
    topic: str
    color: str
    teleop_role: str
    avatar_model: Optional[str]
    avatar_scale: float
    avatar_rotation_deg_xyz: Tuple[float, float, float]


@dataclass
class CameraSpec:
    name: str
    namespace: str


class PoseBridgeNode(LiveAlignmentMixin, Node):
    def __init__(
        self,
        config_path: Path,
        ros_domain_id: int,
        fake_pose: bool = False,
        pose_publish_hz: float = 30.0,
        enable_alignment_stream: bool = False,
    ) -> None:
        if rclpy is None:
            raise RuntimeError("rclpy is required to run the web dashboard backend")
        super().__init__("insight_multi_camera_dashboard_web")
        self.config_path = config_path
        self.ros_domain_id = int(ros_domain_id)
        self.fake_pose = bool(fake_pose)
        self.pose_publish_hz = max(1.0, float(pose_publish_hz))
        self.enable_alignment_stream = bool(enable_alignment_stream)
        self.max_points = 300
        self.pose_timeout_sec = 0.5

        raw_config = load_setup(config_path)
        config = build_dashboard_config(raw_config)
        enabled_camera_map = {
            camera["name"]: camera for camera in raw_config.get("cameras", []) if camera.get("enabled", True)
        }
        self.project_root = config_path.resolve().parents[1]
        self.window_title = config.get("window_title", "Insight Web Dashboard")
        self.image_qos_reliability = str(config.get("trajectory", {}).get("image_qos_reliability", "best_effort"))
        self._configure_live_alignment(raw_config, config)

        self.cameras: List[CameraSpec] = [
            CameraSpec(
                name=item["name"],
                namespace=enabled_camera_map[item["name"]]["namespace"],
            )
            for item in config.get("cameras", [])
        ]
        self.poses: List[PoseSpec] = [
            PoseSpec(
                name=item["name"],
                topic=item["topic"],
                color=item["color"],
                teleop_role=str(item.get("teleop_role", item["name"])),
                avatar_model=item.get("avatar_model"),
                avatar_scale=float(item.get("avatar_scale", 1.0)),
                avatar_rotation_deg_xyz=tuple(float(value) for value in item.get("avatar_rotation_deg_xyz", [0.0, 0.0, 0.0])),
            )
            for item in config.get("poses", [])
        ]
        if self.reference_camera is None and self.poses:
            self.reference_camera = self.poses[0].name

        self.raw_traces: Dict[str, List[Tuple[float, float, float]]] = {pose.name: [] for pose in self.poses}
        self.latest_pose: Dict[str, Optional[Tuple[float, float, float]]] = {pose.name: None for pose in self.poses}
        self.latest_pose_sample: Dict[str, Optional[PoseSample]] = {pose.name: None for pose in self.poses}
        self.last_pose_received_time: Dict[str, float] = {pose.name: 0.0 for pose in self.poses}
        self.pose_history: Dict[str, Deque[PoseSample]] = {pose.name: deque(maxlen=160) for pose in self.poses}
        self.pose_history_lock = threading.Lock()
        self.pose_lock = threading.Lock()
        self.live_alignment_image_lock = threading.Lock()
        self.live_alignment_solution_lock = threading.Lock()
        self.ros_callback_group = ReentrantCallbackGroup()
        self.dashboard_subscriptions = []
        self._initialize_live_alignment_state()
        if self.world_to_reference:
            self.get_logger().info("Loaded persisted live alignment state for web dashboard startup")

        if self.fake_pose:
            self.create_timer(1.0 / self.pose_publish_hz, self._update_fake_pose, callback_group=self.ros_callback_group)
            self.get_logger().info("Running in fake-pose demo mode")
        else:
            self._create_pose_subscriptions()
            if self.enable_alignment_stream:
                self._create_alignment_subscriptions()

        if self.enable_alignment_stream and self.live_alignment_available:
            self.live_alignment_timer = self.create_timer(
                1.0 / max(self.live_alignment_processing_hz, 0.5),
                self._process_live_alignment,
                callback_group=self.ros_callback_group,
            )

    def _create_pose_subscriptions(self) -> None:
        pose_qos = make_qos()
        for pose in self.poses:
            sub = self.create_subscription(
                PoseStamped,
                pose.topic,
                self._make_pose_callback(pose.name),
                pose_qos,
                callback_group=self.ros_callback_group,
            )
            self.dashboard_subscriptions.append(sub)
            self.get_logger().info(f"Trajectory: {pose.name} <- {pose.topic}")

    def _create_alignment_subscriptions(self) -> None:
        if not self.live_alignment_available:
            return
        image_qos = make_image_qos(reliability=self.image_qos_reliability)
        for camera in self.cameras:
            camera_name = camera.name
            namespace = camera.namespace
            calib_topic = image_topic(namespace, self.live_alignment_image_stream)
            calib_info_topic = camera_info_topic(namespace, self.live_alignment_image_stream)
            calib_type = IMAGE_STREAMS[self.live_alignment_image_stream]["type"]
            self.live_alignment_topic_by_camera[camera_name] = calib_topic

            calib_msg_type = CompressedImage if calib_type == "compressed" else RosImage
            calib_sub = self.create_subscription(
                calib_msg_type,
                calib_topic,
                self._make_live_alignment_image_callback(camera_name, calib_type),
                image_qos,
                callback_group=self.ros_callback_group,
            )
            self.dashboard_subscriptions.append(calib_sub)

            info_sub = self.create_subscription(
                CameraInfo,
                calib_info_topic,
                self._make_camera_info_callback(camera_name),
                make_qos(depth=2),
                callback_group=self.ros_callback_group,
            )
            self.dashboard_subscriptions.append(info_sub)
            self.get_logger().info(
                f"Alignment: {camera_name} image={calib_topic} info={calib_info_topic} type={calib_type}"
            )

    def _make_pose_callback(self, pose_name: str):
        def callback(msg: PoseStamped) -> None:
            pose_sample = PoseSample(
                stamp_ns=self._stamp_to_ns(msg.header.stamp),
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

    def _make_camera_info_callback(self, camera_name: str):
        def callback(msg: CameraInfo) -> None:
            self.live_alignment_camera_matrix[camera_name] = np.array(msg.k, dtype=np.float64).reshape((3, 3))
            self.live_alignment_dist_coeffs[camera_name] = np.array(msg.d, dtype=np.float64).reshape((-1, 1))

        return callback

    def _make_live_alignment_image_callback(self, camera_name: str, topic_type: str):
        def callback(msg) -> None:
            if not self.live_alignment_active:
                return
            image = self._decode_calibration_message(topic_type, msg)
            if image is None:
                self.live_alignment_last_tag_count[camera_name] = 0
                self._set_alignment_debug(camera_name, stage="decode_failed", tags=0, shape="-")
                return
            stamp_ns = self._stamp_to_ns(msg.header.stamp)
            received_monotonic_ns = time.monotonic_ns()
            with self.live_alignment_image_lock:
                self.live_alignment_latest_image[camera_name] = image
                self.live_alignment_latest_image_stamp_ns[camera_name] = stamp_ns
                pending = self.live_alignment_pending_images[camera_name]
                if not pending or pending[-1][0] != stamp_ns:
                    pending.append((stamp_ns, received_monotonic_ns, image))
                    min_received_ns = received_monotonic_ns - self.live_alignment_pending_max_age_ns
                    pending[:] = [item for item in pending if item[1] >= min_received_ns]
                    if len(pending) > self.live_alignment_pending_image_limit:
                        del pending[: len(pending) - self.live_alignment_pending_image_limit]
            self._set_alignment_debug(
                camera_name,
                stage="image_rx",
                stamp_ns=stamp_ns,
                shape=f"{image.shape[1]}x{image.shape[0]}",
                topic=self.live_alignment_topic_by_camera.get(camera_name, "-"),
                latency_ms=f"{0.0:.1f}",
            )

        return callback

    def _record_pose_sample(self, pose_name: str, pose_sample: PoseSample) -> None:
        with self.pose_history_lock:
            self.pose_history[pose_name].append(pose_sample)
        with self.pose_lock:
            self.latest_pose_sample[pose_name] = pose_sample
            self.latest_pose[pose_name] = self._transform_pose_point(pose_name, pose_sample.position)
            self.last_pose_received_time[pose_name] = time.monotonic()
            raw_trace = self.raw_traces[pose_name]
            raw_trace.append(pose_sample.position)
            if len(raw_trace) > self.max_points:
                del raw_trace[: len(raw_trace) - self.max_points]

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
                stamp_ns=time.time_ns(),
                position=(x, y, z),
                orientation_xyzw=quaternion,
            )
            self._record_pose_sample(pose.name, sample)

    def build_pose_payload(self) -> Dict[str, object]:
        now = time.monotonic()
        poses = []
        with self.pose_lock:
            for pose in self.poses:
                transformed = self.transformed_pose_sample(pose.name)
                raw_sample = self.latest_pose_sample.get(pose.name)
                visible = raw_sample is not None and (self.fake_pose or (now - self.last_pose_received_time[pose.name]) <= self.pose_timeout_sec)
                if transformed is None:
                    position = [0.0, 0.0, 0.0]
                    quaternion = [0.0, 0.0, 0.0, 1.0]
                else:
                    position = [float(value) for value in transformed.position]
                    quaternion = [float(value) for value in transformed.orientation_xyzw]
                poses.append(
                    {
                        "name": pose.name,
                        "role": pose.teleop_role,
                        "visible": visible,
                        "position": position,
                        "quaternion_xyzw": quaternion,
                        "trace": [
                            [float(sample[0]), float(sample[1]), float(sample[2])]
                            for sample in self.transformed_trace(pose.name)
                        ],
                        "avatar_model": pose.avatar_model,
                        "avatar_scale": pose.avatar_scale,
                        "avatar_rotation_deg_xyz": [float(value) for value in pose.avatar_rotation_deg_xyz],
                        "gripper_open_ratio": self._gripper_open_ratio_for_pose(pose, now),
                    }
                )
        return {
            "type": "pose_update",
            "timestamp_ms": int(time.time() * 1000),
            "fake_pose": self.fake_pose,
            "poses": poses,
        }

    def model_asset_url(self, avatar_model: Optional[str]) -> Optional[str]:
        if not avatar_model:
            return None
        return f"/asset?path={quote(avatar_model, safe='')}"

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

    def _gripper_open_ratio_for_pose(self, pose: PoseSpec, now: float) -> Optional[float]:
        if pose.teleop_role not in ("left_hand", "right_hand"):
            return None
        if not self.fake_pose:
            return None
        return max(0.0, min(1.0, 0.5 + 0.5 * math.sin(now * 1.8 + self._role_phase(pose.name))))


class WebDashboardServer:
    def __init__(
        self,
        node: PoseBridgeNode,
        host: str,
        port: int,
        web_root: Optional[Path],
        project_root: Path,
        rosbag_store: RosbagStore,
        recording_manager: RecordingManager,
        post_processor: PostProcessor,
    ) -> None:
        self.node = node
        self.host = host
        self.port = int(port)
        self.web_root = web_root.resolve() if web_root else None
        self.project_root = project_root.resolve()
        self.rosbag_store = rosbag_store
        self.recording_manager = recording_manager
        self.post_processor = post_processor
        self._clients: Set[web.WebSocketResponse] = set()
        self._loop = asyncio.new_event_loop()
        self._thread: Optional[threading.Thread] = None
        self._started = threading.Event()
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="web_dashboard_server")
        self._thread.start()
        self._started.wait(timeout=5.0)

    def stop(self) -> None:
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=2.0)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        app = web.Application()
        app.router.add_get("/ws", self._handle_ws)
        app.router.add_get("/healthz", self._handle_healthz)
        app.router.add_get("/api/poses", self._handle_pose_snapshot)
        app.router.add_get("/api/rosbags", self._handle_rosbags)
        app.router.add_get("/api/recording/status", self._handle_recording_status)
        app.router.add_post("/api/recording/start", self._handle_recording_start)
        app.router.add_post("/api/recording/stop", self._handle_recording_stop)
        app.router.add_post("/api/postprocess/{action}", self._handle_postprocess)
        app.router.add_get("/asset", self._handle_asset)
        if self.web_root and self.web_root.exists():
            app.router.add_get("/", self._handle_index)
            app.router.add_get("/3d", self._handle_index)
            static_root = self.web_root / "static"
            if static_root.exists():
                app.router.add_static("/static/", str(static_root), show_index=False)
        app.on_startup.append(self._on_startup)
        app.on_shutdown.append(self._on_shutdown)
        runner = web.AppRunner(app)
        self._loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, self.host, self.port)
        self._loop.run_until_complete(site.start())
        self._started.set()
        self.node.get_logger().info(f"Web dashboard backend listening on http://{self.host}:{self.port}")
        try:
            self._loop.run_forever()
        finally:
            self._loop.run_until_complete(runner.cleanup())
            self._loop.close()

    async def _on_startup(self, app: web.Application) -> None:
        app["broadcast_task"] = asyncio.create_task(self._broadcast_loop())

    async def _on_shutdown(self, app: web.Application) -> None:
        task = app.get("broadcast_task")
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _broadcast_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0 / self.node.pose_publish_hz)
            if not self._clients:
                continue
            payload = self.node.build_pose_payload()
            for pose in payload["poses"]:
                pose["asset_url"] = self.node.model_asset_url(pose.get("avatar_model"))
            stale = []
            for ws in list(self._clients):
                try:
                    await ws.send_json(payload)
                except Exception:
                    stale.append(ws)
            for ws in stale:
                self._clients.discard(ws)

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20.0)
        await ws.prepare(request)
        self._clients.add(ws)
        snapshot = self.node.build_pose_payload()
        for pose in snapshot["poses"]:
            pose["asset_url"] = self.node.model_asset_url(pose.get("avatar_model"))
        await ws.send_json(snapshot)
        async for _message in ws:
            pass
        self._clients.discard(ws)
        return ws

    async def _handle_healthz(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "fake_pose": self.node.fake_pose,
                "ros_domain_id": self.node.ros_domain_id,
            }
        )

    async def _handle_pose_snapshot(self, _request: web.Request) -> web.Response:
        payload = self.node.build_pose_payload()
        for pose in payload["poses"]:
            pose["asset_url"] = self.node.model_asset_url(pose.get("avatar_model"))
        return web.json_response(payload)

    async def _handle_rosbags(self, _request: web.Request) -> web.Response:
        bags = [entry.to_dict() for entry in self.rosbag_store.list_bags()]
        return web.json_response({"rosbag_root": str(self.rosbag_store.root), "bags": bags})

    async def _handle_recording_status(self, _request: web.Request) -> web.Response:
        return web.json_response(self.recording_manager.status())

    async def _handle_recording_start(self, request: web.Request) -> web.Response:
        try:
            payload = await self._json_or_empty(request)
            topics = payload.get("topics")
            if topics is not None and not isinstance(topics, list):
                raise ValueError("topics must be a list")
            status = self.recording_manager.start(topics=topics)
            return web.json_response(status)
        except Exception as exc:
            return self._json_error(exc)

    async def _handle_recording_stop(self, _request: web.Request) -> web.Response:
        try:
            return web.json_response(self.recording_manager.stop())
        except Exception as exc:
            return self._json_error(exc)

    async def _handle_postprocess(self, request: web.Request) -> web.Response:
        try:
            action = request.match_info["action"]
            payload = await self._json_or_empty(request)
            bag_name = str(payload.get("bag", "")).strip()
            if not bag_name:
                raise ValueError("bag is required")
            result = self.post_processor.run(action, bag_name)
            return web.json_response(result)
        except Exception as exc:
            return self._json_error(exc)

    async def _json_or_empty(self, request: web.Request) -> Dict[str, object]:
        if not request.can_read_body:
            return {}
        try:
            payload = await request.json()
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _json_error(exc: Exception, status: int = 400) -> web.Response:
        return web.json_response({"error": str(exc)}, status=status)

    async def _handle_index(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.web_root / "3d.html")

    async def _handle_asset(self, request: web.Request) -> web.StreamResponse:
        raw_path = request.query.get("path", "").strip()
        if not raw_path:
            raise web.HTTPBadRequest(text="missing path")
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = (self.project_root / candidate).resolve()
        else:
            candidate = candidate.resolve()
        allowed_roots = [self.project_root, self.project_root.parent]
        for root in allowed_roots:
            try:
                candidate.relative_to(root)
                break
            except ValueError:
                continue
        else:
            raise web.HTTPForbidden(text="path outside allowed roots")
        if not candidate.is_file():
            raise web.HTTPNotFound(text="asset not found")
        return web.FileResponse(candidate)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path(__file__).resolve().parents[1] / "config" / "cameras.json"))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--web-root", default=str(Path(__file__).resolve().parents[1] / "web_dashboard" / "dist"))
    parser.add_argument("--view-mode", choices=("3d",), default="3d")
    parser.add_argument("--fake-pose", action="store_true")
    parser.add_argument("--pose-publish-hz", type=float, default=30.0)
    parser.add_argument("--start-alignment", action="store_true")
    parser.add_argument(
        "--post-config",
        default=str(Path(__file__).resolve().parents[1] / "config" / "post_processing.json"),
    )
    parser.add_argument("--rosbag-dir", default=os.environ.get("INSIGHT_ROSBAG_DIR", ""))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config_path = Path(args.config).resolve()
    raw_config = load_setup(config_path)
    post_config = load_json_config(Path(args.post_config).resolve())
    rosbag_dir = args.rosbag_dir or os.environ.get("INSIGHT_ROSBAG_DIR") or str(post_config.get("rosbag_dir", "rosbags"))
    rosbag_path = Path(rosbag_dir).expanduser()
    if not rosbag_path.is_absolute():
        rosbag_path = (config_path.parents[1] / rosbag_path).resolve()
    rosbag_topics = post_config.get("record_topics") or build_default_topics(raw_config)
    ros_domain_id = int(raw_config.get("ros_domain_id", 10))
    if rclpy is None:
        raise RuntimeError("rclpy is not available in this environment")

    os.environ.setdefault("ROS_DOMAIN_ID", str(ros_domain_id))
    rclpy.init(args=None)
    enable_alignment_stream = bool(args.start_alignment)
    node = PoseBridgeNode(
        config_path,
        ros_domain_id=ros_domain_id,
        fake_pose=args.fake_pose,
        pose_publish_hz=args.pose_publish_hz,
        enable_alignment_stream=enable_alignment_stream,
    )
    node.get_logger().info(f"View mode={args.view_mode} alignment_stream={enable_alignment_stream}")
    if args.start_alignment and node.live_alignment_available and not args.fake_pose:
        node.start_live_alignment()
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    spin_thread = threading.Thread(target=executor.spin, daemon=True, name="ros_executor")
    spin_thread.start()

    web_root = Path(args.web_root) if args.web_root else None
    rosbag_store = RosbagStore(rosbag_path)
    recording_manager = RecordingManager(
        rosbag_store,
        topics=[str(topic) for topic in rosbag_topics],
        max_cache_size=int(post_config.get("max_cache_size", 2147483648)),
    )
    post_processor = PostProcessor(rosbag_store)
    server = WebDashboardServer(
        node,
        args.host,
        args.port,
        web_root,
        node.project_root,
        rosbag_store,
        recording_manager,
        post_processor,
    )
    server.start()

    try:
        while rclpy.ok():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        with contextlib.suppress(Exception):
            recording_manager.stop()
        server.stop()
        executor.shutdown()
        node.destroy_node()
        with contextlib.suppress(Exception):
            rclpy.shutdown()
        spin_thread.join(timeout=1.0)


if __name__ == "__main__":
    main()
