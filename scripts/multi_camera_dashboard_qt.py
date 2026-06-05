#!/usr/bin/env python3

import argparse
import math
import os
import queue
import statistics
import threading
import time
from io import BytesIO
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import rclpy
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import QLibraryInfo
from geometry_msgs.msg import PoseStamped
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import CompressedImage, Image as RosImage

from camera_setup import build_dashboard_config, load_setup

os.environ["QT_QPA_PLATFORM"] = os.environ.get("QT_QPA_PLATFORM", "xcb")
os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = QLibraryInfo.location(QLibraryInfo.PluginsPath)
os.environ.pop("QT_PLUGIN_PATH", None)

import numpy as np


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
class CameraSpec:
    name: str
    label: str
    topic: str
    topic_type: str
    row: int
    column: int
    column_span: int
    row_span: int


@dataclass
class PoseSpec:
    name: str
    topic: str
    color: str


class DashboardNode(Node):
    def __init__(self, config_path: Path) -> None:
        super().__init__("insight_multi_camera_dashboard_qt")

        config = build_dashboard_config(load_setup(config_path))
        self.window_title = config.get("window_title", "Insight Dashboard")
        self.fullscreen = bool(config.get("fullscreen", True))
        self.max_points = int(config.get("trajectory", {}).get("max_points", 1500))
        self.view_yaw_deg = float(config.get("trajectory", {}).get("view_yaw_deg", -35))
        self.view_pitch_deg = float(config.get("trajectory", {}).get("view_pitch_deg", 28))
        self.ui_refresh_ms = int(config.get("trajectory", {}).get("ui_refresh_ms", 100))
        self.image_decode_reduction = int(config.get("trajectory", {}).get("image_decode_reduction", 4))
        self.display_fps_limit = float(config.get("trajectory", {}).get("display_fps_limit", 6))
        self.image_qos_reliability = str(config.get("trajectory", {}).get("image_qos_reliability", "best_effort"))
        self.trajectory_title = config.get("trajectory", {}).get("title", "3D VIO Trajectory")
        self.trajectory_subtitle = config.get("trajectory", {}).get(
            "subtitle", "Interactive 3D view of x/y/z using current VIO poses."
        )

        self.cameras: List[CameraSpec] = [
            CameraSpec(
                name=item["name"],
                label=item["label"],
                topic=item["topic"],
                topic_type=item["type"],
                row=int(item.get("row", 0)),
                column=int(item.get("column", 0)),
                column_span=int(item.get("column_span", 1)),
                row_span=int(item.get("row_span", 1)),
            )
            for item in config.get("cameras", [])
        ]
        self.poses: List[PoseSpec] = [
            PoseSpec(name=item["name"], topic=item["topic"], color=item["color"])
            for item in config.get("poses", [])
        ]

        self.latest_images: Dict[str, Optional[np.ndarray]] = {camera.name: None for camera in self.cameras}
        self.image_versions: Dict[str, int] = {camera.name: 0 for camera in self.cameras}
        self.image_lock = threading.Lock()
        self.last_frame_received_time: Dict[str, float] = {camera.name: 0.0 for camera in self.cameras}
        self.last_frame_decoded_time: Dict[str, float] = {camera.name: 0.0 for camera in self.cameras}
        self.frame_received_count: Dict[str, int] = {camera.name: 0 for camera in self.cameras}
        self.frame_decoded_count: Dict[str, int] = {camera.name: 0 for camera in self.cameras}
        self.decode_durations_ms: Dict[str, List[float]] = {camera.name: [] for camera in self.cameras}
        self.debug_lock = threading.Lock()
        self.probe_received_count: Dict[str, int] = {camera.name: 0 for camera in self.cameras}
        self.probe_last_time: Dict[str, float] = {camera.name: 0.0 for camera in self.cameras}

        self.pending_messages: Dict[str, Optional[object]] = {camera.name: None for camera in self.cameras}
        self.pending_lock = threading.Lock()
        self.decoder_events: Dict[str, threading.Event] = {camera.name: threading.Event() for camera in self.cameras}
        self.decoder_stop_event = threading.Event()
        self.decoder_threads: List[threading.Thread] = []

        self.traces: Dict[str, List[Tuple[float, float, float]]] = {pose.name: [] for pose in self.poses}
        self.latest_pose: Dict[str, Optional[Tuple[float, float, float]]] = {pose.name: None for pose in self.poses}
        self.pose_versions: Dict[str, int] = {pose.name: 0 for pose in self.poses}
        self.dashboard_subscriptions = []

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
        pose_qos = make_qos()

        for camera in self.cameras:
            if camera.topic_type == "compressed":
                sub = self.create_subscription(
                    CompressedImage,
                    camera.topic,
                    self._make_compressed_callback(camera.name),
                    image_qos,
                )
            else:
                sub = self.create_subscription(
                    RosImage,
                    camera.topic,
                    self._make_image_callback(camera.name),
                    image_qos,
                )
            self.dashboard_subscriptions.append(sub)
            self.get_logger().info(
                f"Image: {camera.label} <- {camera.topic} "
                f"(qos={self.image_qos_reliability}, depth={image_qos.depth})"
            )
            if camera.name == "insight9_a":
                probe_sub = self.create_subscription(
                    CompressedImage if camera.topic_type == "compressed" else RosImage,
                    camera.topic,
                    self._make_probe_callback(camera.name),
                    image_qos,
                )
                self.dashboard_subscriptions.append(probe_sub)
                self.get_logger().info(f"Probe: {camera.name} <- {camera.topic}")

        for pose in self.poses:
            sub = self.create_subscription(
                PoseStamped,
                pose.topic,
                self._make_pose_callback(pose.name),
                pose_qos,
            )
            self.dashboard_subscriptions.append(sub)
            self.get_logger().info(f"Trajectory: {pose.name} <- {pose.topic}")

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
            point = (
                float(msg.pose.position.x),
                float(msg.pose.position.y),
                float(msg.pose.position.z),
            )
            trace = self.traces[pose_name]
            trace.append(point)
            if len(trace) > self.max_points:
                del trace[: len(trace) - self.max_points]
            self.latest_pose[pose_name] = point
            self.pose_versions[pose_name] += 1

        return callback

    def _make_probe_callback(self, camera_name: str):
        def callback(_msg: object) -> None:
            with self.debug_lock:
                self.probe_received_count[camera_name] += 1
                self.probe_last_time[camera_name] = time.monotonic()

        return callback

    def _queue_frame(self, camera_name: str, msg: object) -> None:
        with self.debug_lock:
            self.last_frame_received_time[camera_name] = time.monotonic()
            self.frame_received_count[camera_name] += 1
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
                decode_start = time.monotonic()
                image = self._decode_message(camera, msg)
                decode_ms = (time.monotonic() - decode_start) * 1000.0
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
                self.frame_decoded_count[camera.name] += 1
                durations = self.decode_durations_ms[camera.name]
                durations.append(decode_ms)
                if len(durations) > 60:
                    del durations[: len(durations) - 60]

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


class ImagePanel(QtWidgets.QFrame):
    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.setStyleSheet(
            "QFrame { background: #16202a; border: 1px solid #233142; border-radius: 6px; }"
            "QLabel { color: #e9eef4; }"
        )
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        self.title_label = QtWidgets.QLabel(title)
        self.title_label.setStyleSheet("font-size: 16px; font-weight: 600; color: #e9eef4; border: none;")
        layout.addWidget(self.title_label)

        self.fps_label = QtWidgets.QLabel("waiting")
        self.fps_label.setStyleSheet("font-size: 12px; color: #8fa3b8; border: none;")
        layout.addWidget(self.fps_label)

        self.image_label = QtWidgets.QLabel()
        self.image_label.setAlignment(QtCore.Qt.AlignCenter)
        self.image_label.setMinimumSize(320, 220)
        self.image_label.setStyleSheet("background: #0c1116; border: none;")
        self.image_label.setText("Waiting for image...")
        layout.addWidget(self.image_label, 1)


class TrajectoryWidget(QtWidgets.QWidget):
    def __init__(self, app_ref, parent=None) -> None:
        super().__init__(parent)
        self.app_ref = app_ref
        self.setMinimumSize(420, 320)
        self.setMouseTracking(True)

    def paintEvent(self, _event) -> None:
        painter = QtGui.QPainter(self)
        try:
            painter.fillRect(self.rect(), QtGui.QColor("#10161d"))
            painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
            self.app_ref.paint_trajectory(painter, self.width(), self.height())
        except KeyboardInterrupt:
            return
        except Exception as exc:
            print(f"trajectory paint error: {exc}")
        finally:
            painter.end()

    def mousePressEvent(self, event) -> None:
        if event.button() == QtCore.Qt.LeftButton:
            self.app_ref.on_traj_press(event.x(), event.y())

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() & QtCore.Qt.LeftButton:
            self.app_ref.on_traj_drag(event.x(), event.y())
            self.update()

    def mouseReleaseEvent(self, _event) -> None:
        self.app_ref.on_traj_release()

    def wheelEvent(self, event) -> None:
        self.app_ref.on_traj_zoom(event.angleDelta().y())
        self.update()


class DashboardWindow(QtWidgets.QMainWindow):
    BG = "#0f1720"
    TEXT = "#e9eef4"
    MUTED = "#8fa3b8"

    def __init__(self, node: DashboardNode, executor: SingleThreadedExecutor) -> None:
        super().__init__()
        self.node = node
        self.executor = executor
        self.setWindowTitle(node.window_title)
        self.resize(1580, 920)
        self.setMinimumSize(1320, 820)
        self.view_yaw_deg = node.view_yaw_deg
        self.view_pitch_deg = node.view_pitch_deg
        self.view_zoom = 1.0
        self.drag_last_xy: Optional[Tuple[int, int]] = None
        self.last_image_versions: Dict[str, int] = {camera.name: -1 for camera in self.node.cameras}
        self.last_image_render_time: Dict[str, float] = {camera.name: 0.0 for camera in self.node.cameras}
        self.image_display_fps: Dict[str, float] = {camera.name: 0.0 for camera in self.node.cameras}
        self.last_displayed_image_time: Dict[str, float] = {camera.name: 0.0 for camera in self.node.cameras}
        self.last_debug_counts: Dict[str, Tuple[int, int]] = {camera.name: (0, 0) for camera in self.node.cameras}
        self.last_probe_counts: Dict[str, int] = {camera.name: 0 for camera in self.node.cameras}
        self.ui_render_durations_ms: Dict[str, List[float]] = {camera.name: [] for camera in self.node.cameras}
        self.last_ui_refresh_started: Dict[str, float] = {camera.name: 0.0 for camera in self.node.cameras}
        self.last_ui_refresh_completed: Dict[str, float] = {camera.name: 0.0 for camera in self.node.cameras}
        self.ui_skip_count: Dict[str, int] = {camera.name: 0 for camera in self.node.cameras}
        self.last_trajectory_refresh_started: float = 0.0
        self.last_trajectory_refresh_completed: float = 0.0
        self.trajectory_render_durations_ms: List[float] = []
        self.pose_visibility: Dict[str, bool] = {pose.name: True for pose in self.node.poses}
        self.image_timers: Dict[str, QtCore.QTimer] = {}
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

        title = QtWidgets.QLabel("Insight Monitoring Dashboard")
        title.setStyleSheet("font-size: 26px; font-weight: 700; color: #e9eef4;")
        root.addWidget(title)

        subtitle = QtWidgets.QLabel("Left: all image feeds. Right: GPU-friendlier Qt view for live VIO trajectories.")
        subtitle.setStyleSheet("font-size: 12px; color: #8fa3b8;")
        root.addWidget(subtitle)

        body = QtWidgets.QHBoxLayout()
        body.setSpacing(12)
        root.addLayout(body, 1)

        image_grid_widget = QtWidgets.QWidget()
        image_grid = QtWidgets.QGridLayout(image_grid_widget)
        image_grid.setContentsMargins(0, 0, 0, 0)
        image_grid.setSpacing(12)
        body.addWidget(image_grid_widget, 4)

        self.image_panels: Dict[str, ImagePanel] = {}
        for camera in self.node.cameras:
            panel = ImagePanel(camera.label)
            image_grid.addWidget(panel, camera.row, camera.column, camera.row_span, camera.column_span)
            self.image_panels[camera.name] = panel

        right = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(12)
        body.addWidget(right, 2)

        traj_frame = QtWidgets.QFrame()
        traj_frame.setStyleSheet("QFrame { background: #16202a; border: 1px solid #233142; border-radius: 6px; }")
        traj_layout = QtWidgets.QVBoxLayout(traj_frame)
        traj_layout.setContentsMargins(14, 12, 14, 12)
        traj_layout.setSpacing(8)
        right_layout.addWidget(traj_frame, 5)

        traj_title = QtWidgets.QLabel(self.node.trajectory_title)
        traj_title.setStyleSheet("font-size: 18px; font-weight: 700; color: #e9eef4;")
        traj_layout.addWidget(traj_title)

        traj_subtitle = QtWidgets.QLabel(self.node.trajectory_subtitle)
        traj_subtitle.setStyleSheet("font-size: 11px; color: #8fa3b8;")
        traj_layout.addWidget(traj_subtitle)

        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(QtWidgets.QLabel("Show:"))
        self.pose_menu_button = QtWidgets.QToolButton()
        self.pose_menu_button.setText("Select cameras")
        self.pose_menu_button.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.pose_menu = QtWidgets.QMenu(self)
        self.pose_actions: Dict[str, QtWidgets.QAction] = {}
        for pose in self.node.poses:
            action = QtWidgets.QAction(pose.name, self, checkable=True, checked=True)
            action.toggled.connect(self._make_pose_toggle(pose.name))
            self.pose_menu.addAction(action)
            self.pose_actions[pose.name] = action
        self.pose_menu_button.setMenu(self.pose_menu)
        controls.addWidget(self.pose_menu_button)
        controls.addStretch(1)
        traj_layout.addLayout(controls)

        self.traj_widget = TrajectoryWidget(self)
        traj_layout.addWidget(self.traj_widget, 1)

        status_frame = QtWidgets.QFrame()
        status_frame.setStyleSheet("QFrame { background: #16202a; border: 1px solid #233142; border-radius: 6px; }")
        status_layout = QtWidgets.QVBoxLayout(status_frame)
        status_layout.setContentsMargins(14, 12, 14, 12)
        status_layout.setSpacing(8)
        right_layout.addWidget(status_frame, 2)

        status_title = QtWidgets.QLabel("Trajectory Status")
        status_title.setStyleSheet("font-size: 16px; font-weight: 700; color: #e9eef4;")
        status_layout.addWidget(status_title)

        self.pose_labels: Dict[str, QtWidgets.QLabel] = {}
        for pose in self.node.poses:
            row = QtWidgets.QHBoxLayout()
            dot = QtWidgets.QLabel()
            dot.setFixedSize(12, 12)
            dot.setStyleSheet(f"background: {pose.color}; border-radius: 6px;")
            row.addWidget(dot)
            label = QtWidgets.QLabel(f"{pose.name}: waiting")
            label.setStyleSheet("font-size: 12px; color: #e9eef4;")
            row.addWidget(label)
            row.addStretch(1)
            status_layout.addLayout(row)
            self.pose_labels[pose.name] = label

    def _setup_timers(self) -> None:
        fps = max(1.0, float(self.node.display_fps_limit))
        interval_ms = max(16, int(1000.0 / fps))
        for idx, camera in enumerate(self.node.cameras):
            timer = QtCore.QTimer(self)
            timer.timeout.connect(lambda cam_name=camera.name: self.refresh_image(cam_name))
            timer.start(interval_ms + (idx * 5))
            self.image_timers[camera.name] = timer

        self.refresh_timer = QtCore.QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_trajectory)
        self.refresh_timer.start(max(50, self.node.ui_refresh_ms))

        self.debug_timer = QtCore.QTimer(self)
        self.debug_timer.timeout.connect(self.print_debug_status)
        self.debug_timer.start(1000)

    def _make_pose_toggle(self, pose_name: str):
        def handler(checked: bool) -> None:
            self.pose_visibility[pose_name] = checked
            self.traj_widget.update()

        return handler

    def start(self) -> None:
        self.spin_thread.start()
        if self.node.fullscreen:
            self.showFullScreen()
        else:
            self.show()

    def closeEvent(self, event) -> None:
        for timer in self.image_timers.values():
            timer.stop()
        self.refresh_timer.stop()
        self.debug_timer.stop()
        self.node.shutdown_workers()
        self.executor.shutdown()
        self.node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        event.accept()

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        if event.key() == QtCore.Qt.Key_Escape and self.isFullScreen():
            self.showNormal()
            return
        super().keyPressEvent(event)

    def refresh_image(self, camera_name: str) -> None:
        try:
            now = time.monotonic()
            self.last_ui_refresh_started[camera_name] = now
            with self.node.image_lock:
                image = self.node.latest_images[camera_name]
                version = self.node.image_versions[camera_name]
            panel = self.image_panels[camera_name]
            if image is None:
                panel.fps_label.setText("waiting")
                self.last_ui_refresh_completed[camera_name] = time.monotonic()
                return
            if version == self.last_image_versions[camera_name] and panel.image_label.pixmap() is not None:
                self.ui_skip_count[camera_name] += 1
                panel.fps_label.setText(f"{self.image_display_fps[camera_name]:.1f} FPS")
                self.last_ui_refresh_completed[camera_name] = time.monotonic()
                return

            pixmap = self._pixmap_from_image(image, panel.image_label.size())
            panel.image_label.setPixmap(pixmap)
            previous = self.last_image_render_time[camera_name]
            if previous > 0:
                inst_fps = 1.0 / max(now - previous, 1e-6)
                self.image_display_fps[camera_name] = (
                    inst_fps if self.image_display_fps[camera_name] <= 0
                    else 0.7 * self.image_display_fps[camera_name] + 0.3 * inst_fps
                )
            self.last_image_render_time[camera_name] = now
            self.last_displayed_image_time[camera_name] = now
            self.last_image_versions[camera_name] = version
            panel.fps_label.setText(f"{self.image_display_fps[camera_name]:.1f} FPS")
            end = time.monotonic()
            self.last_ui_refresh_completed[camera_name] = end
            render_ms = (end - now) * 1000.0
            samples = self.ui_render_durations_ms[camera_name]
            samples.append(render_ms)
            if len(samples) > 60:
                del samples[: len(samples) - 60]
        except KeyboardInterrupt:
            self.close()

    def refresh_trajectory(self) -> None:
        try:
            start = time.monotonic()
            self.last_trajectory_refresh_started = start
            for pose in self.node.poses:
                visible = self.pose_visibility[pose.name]
                latest = self.node.latest_pose[pose.name]
                if not visible:
                    self.pose_labels[pose.name].setText(f"{pose.name}: hidden")
                elif latest is None:
                    self.pose_labels[pose.name].setText(f"{pose.name}: waiting for VIO")
                else:
                    self.pose_labels[pose.name].setText(
                        f"{pose.name}: x={latest[0]:.2f}, y={latest[1]:.2f}, z={latest[2]:.2f}"
                    )
            self.traj_widget.update()
            end = time.monotonic()
            self.last_trajectory_refresh_completed = end
            render_ms = (end - start) * 1000.0
            self.trajectory_render_durations_ms.append(render_ms)
            if len(self.trajectory_render_durations_ms) > 60:
                del self.trajectory_render_durations_ms[: len(self.trajectory_render_durations_ms) - 60]
        except KeyboardInterrupt:
            self.close()

    def print_debug_status(self) -> None:
        try:
            now = time.monotonic()
            parts: List[str] = []
            with self.node.debug_lock:
                for camera in self.node.cameras:
                    recv_count = self.node.frame_received_count[camera.name]
                    dec_count = self.node.frame_decoded_count[camera.name]
                    probe_count = self.node.probe_received_count[camera.name]
                    prev_recv, prev_dec = self.last_debug_counts[camera.name]
                    recv_delta = recv_count - prev_recv
                    dec_delta = dec_count - prev_dec
                    self.last_debug_counts[camera.name] = (recv_count, dec_count)
                    probe_prev = self.last_probe_counts[camera.name]
                    probe_delta = probe_count - probe_prev
                    self.last_probe_counts[camera.name] = probe_count
                    recv_age = now - self.node.last_frame_received_time[camera.name] if self.node.last_frame_received_time[camera.name] > 0 else -1.0
                    dec_age = now - self.node.last_frame_decoded_time[camera.name] if self.node.last_frame_decoded_time[camera.name] > 0 else -1.0
                    probe_age = now - self.node.probe_last_time[camera.name] if self.node.probe_last_time[camera.name] > 0 else -1.0
                    disp_age = now - self.last_displayed_image_time[camera.name] if self.last_displayed_image_time[camera.name] > 0 else -1.0
                    decode_samples = list(self.node.decode_durations_ms[camera.name])
                    decode_avg = statistics.fmean(decode_samples) if decode_samples else 0.0
                    decode_max = max(decode_samples) if decode_samples else 0.0
                    ui_samples = list(self.ui_render_durations_ms[camera.name])
                    ui_avg = statistics.fmean(ui_samples) if ui_samples else 0.0
                    ui_max = max(ui_samples) if ui_samples else 0.0
                    ui_busy_age = now - self.last_ui_refresh_started[camera.name] if self.last_ui_refresh_started[camera.name] > 0 else -1.0
                    ui_done_age = now - self.last_ui_refresh_completed[camera.name] if self.last_ui_refresh_completed[camera.name] > 0 else -1.0
                    skipped = self.ui_skip_count[camera.name]
                    self.ui_skip_count[camera.name] = 0
                    pending = 0
                    with self.node.pending_lock:
                        pending = 1 if self.node.pending_messages[camera.name] is not None else 0
                    parts.append(
                        f"{camera.name}: recv+{recv_delta}/s total={recv_count} age={recv_age:.2f}s | "
                        f"probe+{probe_delta}/s total={probe_count} age={probe_age:.2f}s | "
                        f"dec+{dec_delta}/s total={dec_count} age={dec_age:.2f}s avg={decode_avg:.1f}ms max={decode_max:.1f}ms | "
                        f"ver={self.node.image_versions[camera.name]} disp_fps={self.image_display_fps[camera.name]:.1f} disp_age={disp_age:.2f}s pending={pending} | "
                        f"ui_avg={ui_avg:.1f}ms ui_max={ui_max:.1f}ms ui_busy_age={ui_busy_age:.2f}s ui_done_age={ui_done_age:.2f}s skips={skipped}"
                    )
            traj_avg = statistics.fmean(self.trajectory_render_durations_ms) if self.trajectory_render_durations_ms else 0.0
            traj_max = max(self.trajectory_render_durations_ms) if self.trajectory_render_durations_ms else 0.0
            traj_busy_age = now - self.last_trajectory_refresh_started if self.last_trajectory_refresh_started > 0 else -1.0
            traj_done_age = now - self.last_trajectory_refresh_completed if self.last_trajectory_refresh_completed > 0 else -1.0
            if parts:
                print(
                    "[dashboard-debug] "
                    + " || ".join(parts)
                    + f" || traj_ui_avg={traj_avg:.1f}ms traj_ui_max={traj_max:.1f}ms "
                      f"traj_busy_age={traj_busy_age:.2f}s traj_done_age={traj_done_age:.2f}s",
                    flush=True,
                )
        except Exception as exc:
            print(f"[dashboard-debug] failed to print status: {exc}", flush=True)

    def _pixmap_from_image(self, image: np.ndarray, target_size: QtCore.QSize) -> QtGui.QPixmap:
        image = np.ascontiguousarray(image, dtype=np.uint8)
        h, w = image.shape[:2]
        bytes_per_line = w * 3
        qimage = QtGui.QImage(image.data, w, h, bytes_per_line, QtGui.QImage.Format_RGB888).copy()
        pixmap = QtGui.QPixmap.fromImage(qimage)
        return pixmap.scaled(
            max(target_size.width(), 10),
            max(target_size.height(), 10),
            QtCore.Qt.KeepAspectRatio,
            QtCore.Qt.FastTransformation,
        )

    def on_traj_press(self, x: int, y: int) -> None:
        self.drag_last_xy = (x, y)

    def on_traj_drag(self, x: int, y: int) -> None:
        if self.drag_last_xy is None:
            self.drag_last_xy = (x, y)
            return
        dx = x - self.drag_last_xy[0]
        dy = y - self.drag_last_xy[1]
        self.drag_last_xy = (x, y)
        self.view_yaw_deg += dx * 0.5
        self.view_pitch_deg = max(-85.0, min(85.0, self.view_pitch_deg - dy * 0.4))

    def on_traj_release(self) -> None:
        self.drag_last_xy = None

    def on_traj_zoom(self, delta: int) -> None:
        if delta > 0:
            self.view_zoom *= 1.1
        elif delta < 0:
            self.view_zoom /= 1.1
        self.view_zoom = max(0.35, min(3.5, self.view_zoom))

    def paint_trajectory(self, painter: QtGui.QPainter, width: int, height: int) -> None:
        visible_pose_names = [pose.name for pose in self.node.poses if self.pose_visibility[pose.name]]
        all_points = [p for pose in self.node.poses if pose.name in visible_pose_names for p in self.node.traces[pose.name]]
        if not all_points:
            painter.setPen(QtGui.QColor(self.MUTED))
            painter.drawText(self.rect_for(width, height), QtCore.Qt.AlignCenter, "Select one or more cameras to view trajectory.")
            return

        _, _, scene = self._project_traces(all_points, width, height)
        self._draw_axes(painter, width, height, scene)
        for pose in self.node.poses:
            if pose.name not in visible_pose_names:
                continue
            trace = self.node.traces[pose.name]
            if not trace:
                continue
            points_2d, _, _ = self._project_traces(trace, width, height, scene=scene)
            pen = QtGui.QPen(QtGui.QColor(pose.color))
            pen.setWidth(3)
            painter.setPen(pen)
            for idx in range(1, len(points_2d)):
                painter.drawLine(
                    QtCore.QPointF(points_2d[idx - 1][0], points_2d[idx - 1][1]),
                    QtCore.QPointF(points_2d[idx][0], points_2d[idx][1]),
                )
            brush = QtGui.QBrush(QtGui.QColor(pose.color))
            painter.setBrush(brush)
            x, y = points_2d[-1]
            painter.drawEllipse(QtCore.QPointF(x, y), 5, 5)

    def _project_traces(self, points: List[Tuple[float, float, float]], width: int, height: int, scene=None):
        yaw = math.radians(self.view_yaw_deg)
        pitch = math.radians(self.view_pitch_deg)
        cos_y, sin_y = math.cos(yaw), math.sin(yaw)
        cos_p, sin_p = math.cos(pitch), math.sin(pitch)
        pad = 32
        usable_w = max(width - 2 * pad, 10)
        usable_h = max(height - 2 * pad, 10)

        if scene is None:
            cx = sum(p[0] for p in points) / len(points)
            cy = sum(p[1] for p in points) / len(points)
            cz = sum(p[2] for p in points) / len(points)
            centered = [(x - cx, y - cy, z - cz) for x, y, z in points]
        else:
            cx, cy, cz = scene["center"]
            centered = [(x - cx, y - cy, z - cz) for x, y, z in points]

        rotated = []
        radius = 0.5
        for x, y, z in centered:
            x1 = cos_y * x - sin_y * y
            y1 = sin_y * x + cos_y * y
            z1 = z
            y2 = cos_p * y1 - sin_p * z1
            z2 = sin_p * y1 + cos_p * z1
            rotated.append((x1, y2, z2))
            radius = max(radius, abs(x1), abs(y2), abs(z2))

        if scene is None:
            scene = {"center": (cx, cy, cz), "radius": radius}
        else:
            radius = max(scene["radius"], 0.5)

        center_x = width / 2
        center_y = height / 2
        focal = min(usable_w, usable_h) * 0.95 * self.view_zoom
        camera_distance = radius * 4.0
        projected = []
        for x1, y2, z2 in rotated:
            denom = max(camera_distance - z2, radius * 0.3)
            scale = focal / denom
            projected.append((center_x + x1 * scale, center_y - y2 * scale))

        xs = [p[0] for p in projected]
        ys = [p[1] for p in projected]
        bounds = (min(xs), max(xs), min(ys), max(ys))
        return projected, bounds, scene

    def _draw_axes(self, painter: QtGui.QPainter, width: int, height: int, scene) -> None:
        radius = max(scene["radius"], 0.5)
        axes = [
            ((0.0, 0.0, 0.0), (radius, 0.0, 0.0), "#ff6b6b", "x"),
            ((0.0, 0.0, 0.0), (0.0, radius, 0.0), "#5dade2", "y"),
            ((0.0, 0.0, 0.0), (0.0, 0.0, radius), "#58d68d", "z"),
        ]
        for start, end, color, label in axes:
            projected, _, _ = self._project_traces([start, end], width, height, scene=scene)
            pen = QtGui.QPen(QtGui.QColor(color))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawLine(
                QtCore.QPointF(projected[0][0], projected[0][1]),
                QtCore.QPointF(projected[1][0], projected[1][1]),
            )
            painter.drawText(int(round(projected[1][0] + 8)), int(round(projected[1][1])), label)
        painter.setPen(QtGui.QColor(self.MUTED))
        painter.drawText(28, 22, f"yaw {self.view_yaw_deg:.0f}°, pitch {self.view_pitch_deg:.0f}°, zoom {self.view_zoom:.2f}x")
        painter.drawText(int(width - 170), 22, "drag rotate, wheel zoom")

    @staticmethod
    def rect_for(width: int, height: int) -> QtCore.QRect:
        return QtCore.QRect(0, 0, width, height)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parent.parent / "config" / "cameras.json"),
    )
    args = parser.parse_args()

    rclpy.init()
    node = DashboardNode(Path(args.config))
    executor = SingleThreadedExecutor()
    executor.add_node(node)

    app = QtWidgets.QApplication([])
    window = DashboardWindow(node, executor)
    window.start()
    app.exec_()


if __name__ == "__main__":
    main()
