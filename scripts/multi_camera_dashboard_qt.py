#!/usr/bin/env python3

import argparse
from collections import deque
import os
import signal
import threading
import time
from io import BytesIO
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

import rclpy
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import QLibraryInfo
from geometry_msgs.msg import PoseStamped
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy, DurabilityPolicy
from sensor_msgs.msg import CameraInfo
from sensor_msgs.msg import CompressedImage, Image as RosImage

from camera_setup import IMAGE_STREAMS, build_dashboard_config, camera_info_topic, image_topic, load_setup
from dashboard_widgets import ImagePanel
from live_alignment import LiveAlignmentMixin
from session_alignment import PoseSample

os.environ["QT_QPA_PLATFORM"] = os.environ.get("QT_QPA_PLATFORM", "xcb")
os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = QLibraryInfo.location(QLibraryInfo.PluginsPath)
os.environ.pop("QT_PLUGIN_PATH", None)

import numpy as np


def make_qos(depth: int = 10, reliability: str = "reliable") -> QoSProfile:
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
class CameraSpec:
    name: str
    label: str
    namespace: str
    topic: str
    camera_info_topic: str
    topic_type: str
    rotation_deg: int
    row: int
    column: int
    column_span: int
    row_span: int


@dataclass
class PoseSpec:
    name: str
    topic: str
    color: str
    teleop_role: str


class DashboardNode(LiveAlignmentMixin, Node):
    def __init__(self, config_path: Path) -> None:
        super().__init__("insight_multi_camera_dashboard_qt")

        raw_config = load_setup(config_path)
        config = build_dashboard_config(raw_config)
        enabled_camera_map = {camera["name"]: camera for camera in raw_config.get("cameras", []) if camera.get("enabled", True)}
        self.window_title = config.get("window_title", "Insight Dashboard")
        self.max_points = int(config.get("trajectory", {}).get("max_points", 1500))
        self.image_decode_reduction = int(config.get("trajectory", {}).get("image_decode_reduction", 4))
        self.display_fps_limit = float(config.get("trajectory", {}).get("display_fps_limit", 6))
        self.image_qos_reliability = str(config.get("trajectory", {}).get("image_qos_reliability", "best_effort"))
        self.pose_qos_reliability = str(config.get("trajectory", {}).get("pose_qos_reliability", "best_effort"))
        self._configure_live_alignment(raw_config, config)

        self.cameras: List[CameraSpec] = [
            CameraSpec(
                name=item["name"],
                label=item["label"],
                namespace=enabled_camera_map[item["name"]]["namespace"],
                topic=item["topic"],
                camera_info_topic=item["camera_info_topic"],
                topic_type=item["type"],
                rotation_deg=int(item.get("rotation_deg", 0)),
                row=int(item.get("row", 0)),
                column=int(item.get("column", 0)),
                column_span=int(item.get("column_span", 1)),
                row_span=int(item.get("row_span", 1)),
            )
            for item in config.get("cameras", [])
        ]
        self.poses: List[PoseSpec] = [
            PoseSpec(
                name=item["name"],
                topic=item["topic"],
                color=item["color"],
                teleop_role=str(item.get("teleop_role", item["name"])),
            )
            for item in config.get("poses", [])
        ]
        if self.reference_camera is None and self.poses:
            self.reference_camera = self.poses[0].name

        self.latest_images: Dict[str, Optional[np.ndarray]] = {camera.name: None for camera in self.cameras}
        self.image_versions: Dict[str, int] = {camera.name: 0 for camera in self.cameras}
        self.image_lock = threading.Lock()
        self.last_frame_received_time: Dict[str, float] = {camera.name: 0.0 for camera in self.cameras}
        self.last_frame_decoded_time: Dict[str, float] = {camera.name: 0.0 for camera in self.cameras}
        self.debug_lock = threading.Lock()
        self.pose_history_lock = threading.Lock()
        self.live_alignment_image_lock = threading.Lock()
        self.live_alignment_solution_lock = threading.Lock()
        self.ros_callback_group = ReentrantCallbackGroup()
        self.pending_messages: Dict[str, Optional[object]] = {camera.name: None for camera in self.cameras}
        self.pending_lock = threading.Lock()
        self.decoder_events: Dict[str, threading.Event] = {camera.name: threading.Event() for camera in self.cameras}
        self.decoder_stop_event = threading.Event()
        self.decoder_threads: List[threading.Thread] = []

        self.raw_traces: Dict[str, List[Tuple[float, float, float]]] = {pose.name: [] for pose in self.poses}
        self.latest_pose: Dict[str, Optional[Tuple[float, float, float]]] = {pose.name: None for pose in self.poses}
        self.last_pose_received_time: Dict[str, float] = {pose.name: 0.0 for pose in self.poses}
        self.pose_versions: Dict[str, int] = {pose.name: 0 for pose in self.poses}
        self.pose_history: Dict[str, Deque[PoseSample]] = {pose.name: deque(maxlen=160) for pose in self.poses}
        self.dashboard_subscriptions = []
        self._initialize_live_alignment_state()
        if self.world_to_reference:
            self.get_logger().info("Loaded persisted live alignment state for dashboard startup")

        for camera in self.cameras:
            worker = threading.Thread(
                target=self._decoder_worker,
                args=(camera,),
                daemon=True,
                name=f"{camera.name}_decoder",
            )
            worker.start()
            self.decoder_threads.append(worker)

        image_qos = make_image_qos(reliability=self.image_qos_reliability)
        pose_qos = make_qos(reliability=self.pose_qos_reliability)

        for camera in self.cameras:
            if camera.topic_type == "compressed":
                sub = self.create_subscription(
                    CompressedImage,
                    camera.topic,
                    self._make_compressed_callback(camera.name),
                    image_qos,
                    callback_group=self.ros_callback_group,
                )
            else:
                sub = self.create_subscription(
                    RosImage,
                    camera.topic,
                    self._make_image_callback(camera.name),
                    image_qos,
                    callback_group=self.ros_callback_group,
                )
            self.dashboard_subscriptions.append(sub)
            self.get_logger().info(
                f"Image: {camera.label} <- {camera.topic} "
                f"(qos={self.image_qos_reliability}, depth={image_qos.depth})"
            )
            info_sub = self.create_subscription(
                CameraInfo,
                camera.camera_info_topic,
                self._make_camera_info_callback(camera.name),
                make_qos(depth=2),
                callback_group=self.ros_callback_group,
            )
            self.dashboard_subscriptions.append(info_sub)
            self.get_logger().info(f"CameraInfo: {camera.label} <- {camera.camera_info_topic}")
            if self.live_alignment_available:
                calib_topic = image_topic(camera.namespace, self.live_alignment_image_stream)
                calib_info_topic = camera_info_topic(camera.namespace, self.live_alignment_image_stream)
                calib_type = IMAGE_STREAMS[self.live_alignment_image_stream]["type"]
                self.live_alignment_topic_by_camera[camera.name] = calib_topic
                calib_msg_type = CompressedImage if calib_type == "compressed" else RosImage
                calib_sub = self.create_subscription(
                    calib_msg_type,
                    calib_topic,
                    self._make_live_alignment_image_callback(camera.name, calib_type),
                    make_image_qos(reliability=self.image_qos_reliability),
                    callback_group=self.ros_callback_group,
                )
                self.dashboard_subscriptions.append(calib_sub)
                self.get_logger().info(
                    f"AlignmentImage: {camera.name} <- {calib_topic} "
                    f"(stream={self.live_alignment_image_stream}, type={calib_type})"
                )
                if calib_info_topic != camera.camera_info_topic:
                    calib_info_sub = self.create_subscription(
                        CameraInfo,
                        calib_info_topic,
                        self._make_live_alignment_camera_info_callback(camera.name),
                        make_qos(depth=2),
                        callback_group=self.ros_callback_group,
                    )
                    self.dashboard_subscriptions.append(calib_info_sub)
        for pose in self.poses:
            sub = self.create_subscription(
                PoseStamped,
                pose.topic,
                self._make_pose_callback(pose.name),
                pose_qos,
                callback_group=self.ros_callback_group,
            )
            self.dashboard_subscriptions.append(sub)
            self.get_logger().info(
                f"Trajectory: {pose.name} <- {pose.topic} "
                f"(qos={self.pose_qos_reliability}, depth={pose_qos.depth})"
            )
        self.live_alignment_timer = self.create_timer(
            1.0 / max(self.live_alignment_processing_hz, 0.5),
            self._process_live_alignment,
            callback_group=self.ros_callback_group,
        )

    def _make_image_callback(self, camera_name: str):
        def callback(msg: RosImage) -> None:
            self._queue_frame(camera_name, msg)

        return callback

    def _make_compressed_callback(self, camera_name: str):
        def callback(msg: CompressedImage) -> None:
            self._queue_frame(camera_name, msg)

        return callback

    def _make_pose_callback(self, pose_name: str):
        def callback(msg: PoseStamped) -> None:
            raw_point = (
                float(msg.pose.position.x),
                float(msg.pose.position.y),
                float(msg.pose.position.z),
            )
            pose_sample = PoseSample(
                stamp_ns=self._stamp_to_ns(msg.header.stamp),
                position=raw_point,
                orientation_xyzw=(
                    float(msg.pose.orientation.x),
                    float(msg.pose.orientation.y),
                    float(msg.pose.orientation.z),
                    float(msg.pose.orientation.w),
                ),
            )
            with self.pose_history_lock:
                self.pose_history[pose_name].append(pose_sample)
                pose_history_len = len(self.pose_history[pose_name])
            point = self._transform_pose_point(pose_name, raw_point)
            trace = self.raw_traces[pose_name]
            trace.append(raw_point)
            if len(trace) > self.max_points:
                del trace[: len(trace) - self.max_points]
            self.latest_pose[pose_name] = point
            self.last_pose_received_time[pose_name] = time.monotonic()
            self.pose_versions[pose_name] += 1
            if self.live_alignment_active:
                self._set_alignment_debug(pose_name, pose_history=pose_history_len)

        return callback

    def _make_camera_info_callback(self, camera_name: str):
        def callback(msg: CameraInfo) -> None:
            self.live_alignment_camera_matrix[camera_name] = np.array(msg.k, dtype=np.float64).reshape((3, 3))
            self.live_alignment_dist_coeffs[camera_name] = np.array(msg.d, dtype=np.float64).reshape((-1, 1))

        return callback

    def _make_live_alignment_camera_info_callback(self, camera_name: str):
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

    def _queue_frame(self, camera_name: str, msg: object) -> None:
        with self.debug_lock:
            self.last_frame_received_time[camera_name] = time.monotonic()
        with self.pending_lock:
            self.pending_messages[camera_name] = msg
        self.decoder_events[camera_name].set()

    def _decoder_worker(self, camera: CameraSpec) -> None:
        event = self.decoder_events[camera.name]
        while not self.decoder_stop_event.is_set():
            event.wait(0.2)
            if self.decoder_stop_event.is_set():
                break
            if not event.is_set():
                continue
            event.clear()
            with self.pending_lock:
                msg = self.pending_messages[camera.name]
                self.pending_messages[camera.name] = None
            if msg is None:
                continue

            try:
                image = self._decode_message(camera, msg)
            except Exception as exc:
                self.get_logger().warning(f"Decoder worker failed for {camera.name}: {exc}")
                continue
            if image is None:
                continue

            with self.image_lock:
                self.latest_images[camera.name] = image
                self.image_versions[camera.name] += 1
            with self.debug_lock:
                self.last_frame_decoded_time[camera.name] = time.monotonic()

    def _decode_message(self, camera: CameraSpec, msg: object) -> Optional[np.ndarray]:
        if camera.topic_type == "compressed":
            return self._decode_compressed_pil(msg)
        return self._convert_ros_image(msg)

    def _decode_compressed_pil(self, msg: CompressedImage) -> Optional[np.ndarray]:
        try:
            from PIL import Image
        except Exception:
            return None

        try:
            with Image.open(BytesIO(msg.data)) as img:
                img = img.convert("RGB")
                reduction = max(1, int(self.image_decode_reduction))
                if reduction > 1:
                    new_w = max(1, img.width // reduction)
                    new_h = max(1, img.height // reduction)
                    img = img.resize((new_w, new_h), Image.Resampling.BILINEAR)
                image = np.array(img, dtype=np.uint8)
                return np.ascontiguousarray(image)
        except Exception:
            return None

    def _convert_ros_image(self, msg: RosImage) -> Optional[np.ndarray]:
        if msg.width == 0 or msg.height == 0:
            return None
        data = np.frombuffer(msg.data, dtype=np.uint8)
        encoding = msg.encoding.lower()
        channels_by_encoding = {
            "mono8": 1,
            "8uc1": 1,
            "rgb8": 3,
            "bgr8": 3,
            "rgba8": 4,
            "bgra8": 4,
        }
        channels = channels_by_encoding.get(encoding)
        if channels is None:
            channels = max(msg.step // max(msg.width, 1), 1)
        if msg.step <= 0 or data.size < msg.step * msg.height:
            return None
        row_bytes = msg.step
        image = data[: row_bytes * msg.height].reshape((msg.height, row_bytes))

        if encoding in ("mono8", "8uc1"):
            gray = image[:, : msg.width]
            rgb = np.repeat(gray[:, :, None], 3, axis=2).astype(np.uint8, copy=False)
            return np.ascontiguousarray(rgb)
        if encoding == "rgb8":
            rgb = image[:, : msg.width * 3].reshape((msg.height, msg.width, 3))
            return np.ascontiguousarray(rgb.astype(np.uint8, copy=False))
        if encoding == "bgr8":
            bgr = image[:, : msg.width * 3].reshape((msg.height, msg.width, 3))
            return np.ascontiguousarray(bgr[:, :, ::-1].astype(np.uint8, copy=False))
        if encoding == "rgba8":
            rgba = image[:, : msg.width * 4].reshape((msg.height, msg.width, 4))
            return np.ascontiguousarray(rgba[:, :, :3].astype(np.uint8, copy=False))
        if encoding == "bgra8":
            bgra = image[:, : msg.width * 4].reshape((msg.height, msg.width, 4))
            rgb = bgra[:, :, [2, 1, 0]]
            return np.ascontiguousarray(rgb.astype(np.uint8, copy=False))
        step_channels = max(msg.step // max(msg.width, 1), 1)
        pixel_image = image[:, : msg.width * step_channels].reshape((msg.height, msg.width, step_channels))
        if step_channels >= 3:
            rgb = pixel_image[:, :, :3]
            return np.ascontiguousarray(rgb.astype(np.uint8, copy=False))
        if step_channels == 1:
            gray = pixel_image[:, :, 0]
            rgb = np.repeat(gray[:, :, None], 3, axis=2).astype(np.uint8, copy=False)
            return np.ascontiguousarray(rgb)
        return None

    def shutdown_workers(self) -> None:
        self.decoder_stop_event.set()
        for event in self.decoder_events.values():
            event.set()
        for worker in self.decoder_threads:
            worker.join(timeout=0.5)

    @staticmethod
    def _stamp_to_ns(stamp) -> int:
        return int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)


class DashboardWindow(QtWidgets.QMainWindow):
    BG = "#0f1720"
    TEXT = "#e9eef4"
    MUTED = "#8fa3b8"

    def __init__(self, node: DashboardNode, executor: MultiThreadedExecutor) -> None:
        super().__init__()
        self.node = node
        self.executor = executor
        self._shutdown_done = False
        self.setWindowTitle(node.window_title)
        self.resize(980, 920)
        self.setMinimumSize(860, 820)
        self.setWindowFlag(QtCore.Qt.FramelessWindowHint, True)
        self.window_drag_active = False
        self.window_drag_offset = QtCore.QPoint()
        self.last_image_versions: Dict[str, int] = {camera.name: -1 for camera in self.node.cameras}
        self.last_image_target_sizes: Dict[str, Tuple[int, int]] = {camera.name: (0, 0) for camera in self.node.cameras}
        self.last_image_render_time: Dict[str, float] = {camera.name: 0.0 for camera in self.node.cameras}
        self.image_display_fps: Dict[str, float] = {camera.name: 0.0 for camera in self.node.cameras}
        self.image_timers: Dict[str, QtCore.QTimer] = {}
        self.alignment_state_timer: Optional[QtCore.QTimer] = None
        self.spin_thread = threading.Thread(target=self.executor.spin, daemon=True)
        self._build_ui()
        self._setup_timers()

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        central.setStyleSheet(f"background: {self.BG};")
        self.setCentralWidget(central)

        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(18, 14, 18, 18)
        root.setSpacing(10)

        self.title_bar_widget = QtWidgets.QWidget()
        title_row = QtWidgets.QHBoxLayout(self.title_bar_widget)
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(12)
        root.addWidget(self.title_bar_widget)

        title = QtWidgets.QLabel("Insight Monitoring Dashboard")
        title.setStyleSheet("font-size: 26px; font-weight: 700; color: #e9eef4;")
        title_row.addWidget(title)

        title_row.addStretch(1)

        self.start_calibration_button = QtWidgets.QPushButton("Start Live Alignment")
        self.start_calibration_button.clicked.connect(self.toggle_live_alignment)
        self.start_calibration_button.setToolTip("Toggle in-memory online relative alignment (C)")
        self.start_calibration_button.setStyleSheet(
            "QPushButton { background: #223244; color: #e9eef4; border: 1px solid #31475d; padding: 6px 10px; border-radius: 4px; }"
            "QPushButton:hover { background: #29405a; }"
        )
        self.start_calibration_button.setEnabled(self.node.live_alignment_available)
        title_row.addWidget(self.start_calibration_button)

        subtitle = QtWidgets.QLabel("RGB dashboard for monitoring and live alignment calibration.")
        subtitle.setStyleSheet("font-size: 12px; color: #8fa3b8;")
        root.addWidget(subtitle)

        image_grid_widget = QtWidgets.QWidget()
        image_grid = QtWidgets.QGridLayout(image_grid_widget)
        image_grid.setContentsMargins(0, 0, 0, 0)
        image_grid.setHorizontalSpacing(10)
        image_grid.setVerticalSpacing(10)
        root.addWidget(image_grid_widget, 1)

        self.image_panels: Dict[str, ImagePanel] = {}
        column_weights: Dict[int, int] = {}
        row_weights: Dict[int, int] = {}
        for camera in self.node.cameras:
            panel = ImagePanel(camera.label)
            image_grid.addWidget(panel, camera.row, camera.column, camera.row_span, camera.column_span)
            self.image_panels[camera.name] = panel
            column_weights[camera.column] = max(column_weights.get(camera.column, 1), camera.row_span)
            for row in range(camera.row, camera.row + camera.row_span):
                row_weights[row] = max(row_weights.get(row, 1), camera.column_span)
        for column, weight in column_weights.items():
            image_grid.setColumnStretch(column, max(weight, 1))
        for row, weight in row_weights.items():
            image_grid.setRowStretch(row, max(weight, 1))

    def _setup_timers(self) -> None:
        fps = max(1.0, float(self.node.display_fps_limit))
        interval_ms = max(16, int(1000.0 / fps))
        for idx, camera in enumerate(self.node.cameras):
            timer = QtCore.QTimer(self)
            timer.timeout.connect(lambda cam_name=camera.name: self.refresh_image(cam_name))
            timer.start(interval_ms + (idx * 5))
            self.image_timers[camera.name] = timer
        self.alignment_state_timer = QtCore.QTimer(self)
        self.alignment_state_timer.timeout.connect(self._sync_alignment_controls)
        self.alignment_state_timer.start(250)
        self._sync_alignment_controls()

    def start(self) -> None:
        self.spin_thread.start()
        screen = QtWidgets.QApplication.primaryScreen()
        if screen is not None:
            self._snap_to_left_half(screen)
        self.show()

    def closeEvent(self, event) -> None:
        self._shutdown()
        event.accept()

    def _shutdown(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        for timer in self.image_timers.values():
            timer.stop()
        if self.alignment_state_timer is not None:
            self.alignment_state_timer.stop()
        self.node.shutdown_workers()
        self.executor.shutdown()
        self.node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        if event.key() == QtCore.Qt.Key_Escape and self.isFullScreen():
            self.showNormal()
            return
        if event.key() == QtCore.Qt.Key_C:
            self.toggle_live_alignment()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.LeftButton and self._is_drag_region(event.pos()):
            self.window_drag_active = True
            self.window_drag_offset = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self.window_drag_active and (event.buttons() & QtCore.Qt.LeftButton):
            self.move(event.globalPos() - self.window_drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.LeftButton and self.window_drag_active:
            self.window_drag_active = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.LeftButton and self._is_drag_region(event.pos()):
            screen = self.screen() or QtWidgets.QApplication.primaryScreen()
            if screen is not None:
                self._snap_to_left_half(screen)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _is_drag_region(self, pos: QtCore.QPoint) -> bool:
        if not self.title_bar_widget.geometry().contains(pos):
            return False
        child = self.childAt(pos)
        if isinstance(child, (QtWidgets.QPushButton, QtWidgets.QToolButton, QtWidgets.QAbstractButton)):
            return False
        return True

    def _snap_to_left_half(self, screen: QtGui.QScreen) -> None:
        geometry = screen.geometry()
        target_width = min(960, geometry.width())
        target_height = min(1080, geometry.height())
        self.setGeometry(geometry.x(), geometry.y(), target_width, target_height)

    def refresh_image(self, camera_name: str) -> None:
        try:
            now = time.monotonic()
            with self.node.image_lock:
                image = self.node.latest_images[camera_name]
                version = self.node.image_versions[camera_name]
            panel = self.image_panels[camera_name]
            target_size = panel.image_label.size()
            target_size_key = (max(target_size.width(), 10), max(target_size.height(), 10))
            if image is None:
                panel.fps_label.setText("waiting")
                return
            panel.set_image_shape(image.shape[1], image.shape[0])
            receive_age = self._age_text(self.node.last_frame_received_time[camera_name], now)
            decoded_age = self._age_text(self.node.last_frame_decoded_time[camera_name], now)
            if (
                version == self.last_image_versions[camera_name]
                and self.last_image_target_sizes[camera_name] == target_size_key
                and panel.image_label.pixmap() is not None
            ):
                panel.fps_label.setText(
                    f"{self.image_display_fps[camera_name]:.1f} FPS | rx age {receive_age} | dec age {decoded_age}"
                )
                return

            camera_spec = next(camera for camera in self.node.cameras if camera.name == camera_name)
            pixmap = self._pixmap_from_image(image, target_size, camera_spec.rotation_deg)
            panel.image_label.setPixmap(pixmap)
            previous = self.last_image_render_time[camera_name]
            if previous > 0:
                inst_fps = 1.0 / max(now - previous, 1e-6)
                self.image_display_fps[camera_name] = (
                    inst_fps if self.image_display_fps[camera_name] <= 0
                    else 0.7 * self.image_display_fps[camera_name] + 0.3 * inst_fps
                )
            self.last_image_render_time[camera_name] = now
            self.last_image_versions[camera_name] = version
            self.last_image_target_sizes[camera_name] = target_size_key
            panel.fps_label.setText(
                f"{self.image_display_fps[camera_name]:.1f} FPS | rx age {receive_age} | dec age {decoded_age}"
            )
        except KeyboardInterrupt:
            self.close()

    def toggle_live_alignment(self) -> None:
        if self.node.live_alignment_active:
            self.node.stop_live_alignment()
        else:
            self.node.start_live_alignment()
        self._sync_alignment_controls()

    def _sync_alignment_controls(self) -> None:
        if self.node.live_alignment_active:
            self.start_calibration_button.setText("Stop Live Alignment")
        else:
            self.start_calibration_button.setText("Start Live Alignment")
        self.start_calibration_button.setToolTip(self.node.alignment_status_text())

    @staticmethod
    def _age_text(last_time: float, now: float) -> str:
        if last_time <= 0.0:
            return "-"
        return f"{now - last_time:.1f}s"

    @staticmethod
    def _pixmap_from_image(image: np.ndarray, target_size: QtCore.QSize, rotation_deg: int = 0) -> QtGui.QPixmap:
        image = np.ascontiguousarray(image, dtype=np.uint8)
        height, width = image.shape[:2]
        bytes_per_line = width * 3
        qimage = QtGui.QImage(image.data, width, height, bytes_per_line, QtGui.QImage.Format_RGB888)
        pixmap = QtGui.QPixmap.fromImage(qimage)
        if rotation_deg % 360:
            pixmap = pixmap.transformed(QtGui.QTransform().rotate(rotation_deg), QtCore.Qt.FastTransformation)
        return pixmap.scaled(
            max(target_size.width(), 10),
            max(target_size.height(), 10),
            QtCore.Qt.KeepAspectRatio,
            QtCore.Qt.FastTransformation,
        )

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parent.parent / "config" / "cameras.json"),
    )
    args = parser.parse_args()

    rclpy.init()
    node = DashboardNode(Path(args.config))
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)

    app = QtWidgets.QApplication([])
    window = DashboardWindow(node, executor)
    app.aboutToQuit.connect(window._shutdown)
    signal.signal(signal.SIGINT, lambda *_args: QtCore.QTimer.singleShot(0, app.quit))
    signal.signal(signal.SIGTERM, lambda *_args: QtCore.QTimer.singleShot(0, app.quit))
    signal_timer = QtCore.QTimer()
    signal_timer.timeout.connect(lambda: None)
    signal_timer.start(200)
    window.start()
    app.exec_()


if __name__ == "__main__":
    main()
