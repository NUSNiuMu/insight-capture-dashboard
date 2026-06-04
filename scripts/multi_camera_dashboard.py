#!/usr/bin/env python3

import argparse
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
import rclpy
import tkinter as tk
from PIL import Image, ImageTk
from geometry_msgs.msg import PoseStamped
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import CompressedImage, Image as RosImage

from camera_setup import build_dashboard_config, load_setup


def make_qos(depth: int = 10) -> QoSProfile:
    return QoSProfile(
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.VOLATILE,
        history=HistoryPolicy.KEEP_LAST,
        depth=depth,
    )


def make_image_qos(depth: int = 1) -> QoSProfile:
    return QoSProfile(
        reliability=ReliabilityPolicy.BEST_EFFORT,
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
        super().__init__("insight_multi_camera_dashboard")

        config = build_dashboard_config(load_setup(config_path))

        self.window_title = config.get("window_title", "Insight Dashboard")
        self.fullscreen = bool(config.get("fullscreen", True))
        self.max_points = int(config.get("trajectory", {}).get("max_points", 1500))
        self.view_yaw_deg = float(config.get("trajectory", {}).get("view_yaw_deg", -35))
        self.view_pitch_deg = float(config.get("trajectory", {}).get("view_pitch_deg", 28))
        self.ui_refresh_ms = int(config.get("trajectory", {}).get("ui_refresh_ms", 100))
        self.image_decode_reduction = int(config.get("trajectory", {}).get("image_decode_reduction", 2))
        self.display_fps_limit = float(config.get("trajectory", {}).get("display_fps_limit", 10))
        self.trajectory_title = config.get("trajectory", {}).get("title", "3D VIO Trajectory")
        self.trajectory_subtitle = config.get("trajectory", {}).get(
            "subtitle",
            "Projected 3D view of x/y/z using current VIO poses.",
        )
        self.trajectory_axis_label = config.get("trajectory", {}).get("axis_label", "y/z proj")

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
            PoseSpec(
                name=item["name"],
                topic=item["topic"],
                color=item["color"],
            )
            for item in config.get("poses", [])
        ]

        self.latest_images: Dict[str, Optional[np.ndarray]] = {camera.name: None for camera in self.cameras}
        self.image_stats: Dict[str, str] = {camera.name: "waiting" for camera in self.cameras}
        self.image_versions: Dict[str, int] = {camera.name: 0 for camera in self.cameras}
        self.traces: Dict[str, List[Tuple[float, float, float]]] = {pose.name: [] for pose in self.poses}
        self.latest_pose: Dict[str, Optional[Tuple[float, float, float]]] = {pose.name: None for pose in self.poses}
        self.pose_versions: Dict[str, int] = {pose.name: 0 for pose in self.poses}
        self.dashboard_subscriptions = []

        image_qos = make_image_qos()
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
            self.get_logger().info(f"Image: {camera.label} <- {camera.topic}")

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
            image = self._convert_ros_image(msg)
            if image is not None:
                self.latest_images[camera_name] = image
                self.image_stats[camera_name] = "receiving"
                self.image_versions[camera_name] += 1

        return callback

    def _make_compressed_callback(self, camera_name: str):
        def callback(msg: CompressedImage) -> None:
            np_arr = np.frombuffer(msg.data, dtype=np.uint8)
            decode_mode = cv2.IMREAD_COLOR
            if self.image_decode_reduction >= 8:
                decode_mode = cv2.IMREAD_REDUCED_COLOR_8
            elif self.image_decode_reduction >= 4:
                decode_mode = cv2.IMREAD_REDUCED_COLOR_4
            elif self.image_decode_reduction >= 2:
                decode_mode = cv2.IMREAD_REDUCED_COLOR_2
            frame = cv2.imdecode(np_arr, decode_mode)
            if frame is not None:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                self.latest_images[camera_name] = frame
                self.image_stats[camera_name] = "receiving"
                self.image_versions[camera_name] += 1

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

    def _convert_ros_image(self, msg: RosImage) -> Optional[np.ndarray]:
        if msg.width == 0 or msg.height == 0:
            return None

        data = np.frombuffer(msg.data, dtype=np.uint8)
        encoding = msg.encoding.lower()

        if encoding in ("mono8", "8uc1"):
            image = data.reshape((msg.height, msg.width))
            return cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)

        if encoding in ("rgb8",):
            return data.reshape((msg.height, msg.width, 3))

        if encoding in ("bgr8",):
            image = data.reshape((msg.height, msg.width, 3))
            return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)

        if encoding in ("mono16", "16uc1"):
            depth = np.frombuffer(msg.data, dtype=np.uint16).reshape((msg.height, msg.width))
            normalized = cv2.normalize(depth, None, 0, 255, cv2.NORM_MINMAX)
            normalized = normalized.astype(np.uint8)
            return cv2.applyColorMap(normalized, cv2.COLORMAP_TURBO)[:, :, ::-1]

        step_channels = max(msg.step // msg.width, 1)
        image = data.reshape((msg.height, msg.width, step_channels))
        if step_channels >= 3:
            return image[:, :, :3]
        if step_channels == 1:
            return cv2.cvtColor(image[:, :, 0], cv2.COLOR_GRAY2RGB)
        return None


class DashboardApp:
    BG = "#0f1720"
    PANEL = "#16202a"
    BORDER = "#233142"
    TEXT = "#e9eef4"
    MUTED = "#8fa3b8"

    def __init__(self, node: DashboardNode, executor: SingleThreadedExecutor) -> None:
        self.node = node
        self.executor = executor
        self.root = tk.Tk()
        self.root.title(node.window_title)
        self.root.configure(bg=self.BG)
        self.root.geometry("1580x920")
        self.root.minsize(1320, 820)
        self.root.attributes("-fullscreen", node.fullscreen)
        self.root.bind("<Escape>", self._exit_fullscreen)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.running = True
        self.closed = False
        self.view_yaw_deg = node.view_yaw_deg
        self.view_pitch_deg = node.view_pitch_deg
        self.view_zoom = 1.0
        self.view_dirty = True
        self.drag_last_xy: Optional[Tuple[int, int]] = None
        self.photo_refs: Dict[str, ImageTk.PhotoImage] = {}
        self.pose_visibility_vars: Dict[str, tk.BooleanVar] = {
            pose.name: tk.BooleanVar(value=True) for pose in self.node.poses
        }
        self.last_image_render_time: Dict[str, float] = {camera.name: 0.0 for camera in self.node.cameras}
        self.image_display_fps: Dict[str, float] = {camera.name: 0.0 for camera in self.node.cameras}
        self.last_image_versions: Dict[str, int] = {camera.name: -1 for camera in self.node.cameras}
        self.last_pose_versions: Dict[str, int] = {pose.name: -1 for pose in self.node.poses}
        self.last_panel_sizes: Dict[str, Tuple[int, int]] = {camera.name: (0, 0) for camera in self.node.cameras}
        self.spin_thread = threading.Thread(target=self._spin_ros, daemon=True)

        self._build_layout()

    def _spin_ros(self) -> None:
        self.executor.spin()

    def _build_layout(self) -> None:
        header = tk.Frame(self.root, bg=self.BG)
        header.pack(fill=tk.X, padx=18, pady=(14, 8))
        tk.Label(
            header,
            text="Insight Monitoring Dashboard",
            bg=self.BG,
            fg=self.TEXT,
            font=("Helvetica", 22, "bold"),
        ).pack(anchor="w")
        tk.Label(
            header,
            text="Left: all image feeds. Right: live VIO trajectories for the three cameras.",
            bg=self.BG,
            fg=self.MUTED,
            font=("Helvetica", 11),
        ).pack(anchor="w", pady=(4, 0))

        body = tk.Frame(self.root, bg=self.BG)
        body.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 18))
        body.grid_columnconfigure(0, weight=4)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(0, weight=1)

        self.image_container = tk.Frame(body, bg=self.BG)
        self.image_container.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self.image_container.grid_columnconfigure(0, weight=1)
        self.image_container.grid_columnconfigure(1, weight=1)
        self.image_container.grid_rowconfigure(0, weight=1)
        self.image_container.grid_rowconfigure(1, weight=1)

        self.image_panels = {}
        for camera in self.node.cameras:
            panel = self._create_image_panel(self.image_container, camera.label)
            panel["frame"].grid(
                row=camera.row,
                column=camera.column,
                columnspan=camera.column_span,
                rowspan=camera.row_span,
                sticky="nsew",
                padx=8,
                pady=8,
            )
            self.image_panels[camera.name] = panel

        self.right_container = tk.Frame(body, bg=self.BG)
        self.right_container.grid(row=0, column=1, sticky="nsew")
        self.right_container.grid_rowconfigure(0, weight=5)
        self.right_container.grid_rowconfigure(1, weight=2)
        self.right_container.grid_columnconfigure(0, weight=1)

        traj_frame = tk.Frame(self.right_container, bg=self.PANEL, highlightbackground=self.BORDER, highlightthickness=1)
        traj_frame.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        tk.Label(
            traj_frame,
            text=self.node.trajectory_title,
            bg=self.PANEL,
            fg=self.TEXT,
            font=("Helvetica", 16, "bold"),
        ).pack(anchor="w", padx=14, pady=(12, 0))
        tk.Label(
            traj_frame,
            text=self.node.trajectory_subtitle,
            bg=self.PANEL,
            fg=self.MUTED,
            font=("Helvetica", 10),
        ).pack(anchor="w", padx=14, pady=(4, 10))
        controls_row = tk.Frame(traj_frame, bg=self.PANEL)
        controls_row.pack(fill=tk.X, padx=14, pady=(0, 10))
        tk.Label(
            controls_row,
            text="Show:",
            bg=self.PANEL,
            fg=self.MUTED,
            font=("Helvetica", 10),
        ).pack(side=tk.LEFT)
        self.pose_filter_button = tk.Menubutton(
            controls_row,
            text="Select cameras",
            bg="#223140",
            fg=self.TEXT,
            activebackground="#2b3d50",
            activeforeground=self.TEXT,
            relief=tk.FLAT,
            padx=10,
            pady=4,
        )
        self.pose_filter_menu = tk.Menu(self.pose_filter_button, tearoff=False, bg="#16202a", fg=self.TEXT)
        self.pose_filter_button.configure(menu=self.pose_filter_menu)
        self.pose_filter_button.pack(side=tk.LEFT, padx=(8, 0))
        for pose in self.node.poses:
            self.pose_filter_menu.add_checkbutton(
                label=pose.name,
                variable=self.pose_visibility_vars[pose.name],
                command=self._on_pose_filter_change,
            )
        self.traj_canvas = tk.Canvas(traj_frame, bg="#10161d", highlightthickness=0)
        self.traj_canvas.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))
        self.traj_canvas.bind("<ButtonPress-1>", self._on_traj_press)
        self.traj_canvas.bind("<B1-Motion>", self._on_traj_drag)
        self.traj_canvas.bind("<ButtonRelease-1>", self._on_traj_release)
        self.traj_canvas.bind("<MouseWheel>", self._on_traj_zoom)
        self.traj_canvas.bind("<Button-4>", self._on_traj_zoom)
        self.traj_canvas.bind("<Button-5>", self._on_traj_zoom)

        status_frame = tk.Frame(self.right_container, bg=self.PANEL, highlightbackground=self.BORDER, highlightthickness=1)
        status_frame.grid(row=1, column=0, sticky="nsew", padx=8, pady=8)
        tk.Label(
            status_frame,
            text="Trajectory Status",
            bg=self.PANEL,
            fg=self.TEXT,
            font=("Helvetica", 14, "bold"),
        ).pack(anchor="w", padx=14, pady=(12, 8))
        self.pose_labels = {}
        for pose in self.node.poses:
            row = tk.Frame(status_frame, bg=self.PANEL)
            row.pack(fill=tk.X, padx=14, pady=6)
            tk.Canvas(row, width=14, height=14, bg=self.PANEL, highlightthickness=0).pack(side=tk.LEFT)
            dot = tk.Canvas(row, width=14, height=14, bg=self.PANEL, highlightthickness=0)
            dot.pack(side=tk.LEFT)
            dot.create_oval(2, 2, 12, 12, fill=pose.color, outline="")
            label = tk.Label(row, text=f"{pose.name}: waiting", bg=self.PANEL, fg=self.TEXT, font=("Helvetica", 11))
            label.pack(side=tk.LEFT, padx=(10, 0))
            self.pose_labels[pose.name] = label

    def _create_image_panel(self, parent: tk.Widget, title: str):
        frame = tk.Frame(parent, bg=self.PANEL, highlightbackground=self.BORDER, highlightthickness=1)
        tk.Label(frame, text=title, bg=self.PANEL, fg=self.TEXT, font=("Helvetica", 13, "bold")).pack(anchor="w", padx=12, pady=(10, 0))
        stats = tk.Label(frame, text="waiting", bg=self.PANEL, fg=self.MUTED, font=("Helvetica", 9, "bold"))
        stats.pack(anchor="w", padx=12, pady=(4, 8))
        image_canvas = tk.Canvas(frame, bg="#0c1116", highlightthickness=0)
        image_canvas.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        image_canvas.create_text(40, 40, text="Waiting for image...", anchor="nw", fill=self.MUTED, font=("Helvetica", 12), tags=("placeholder",))
        return {"frame": frame, "stats": stats, "image": image_canvas}

    def on_close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.running = False
        # Hide the window first so close feels immediate, then let Tk exit.
        self.root.withdraw()
        self.root.update_idletasks()
        self.root.after(0, self.root.destroy)

    def _exit_fullscreen(self, _event=None) -> None:
        self.root.attributes("-fullscreen", False)

    def _on_traj_press(self, event) -> None:
        self.drag_last_xy = (event.x, event.y)

    def _on_traj_drag(self, event) -> None:
        if self.drag_last_xy is None:
            self.drag_last_xy = (event.x, event.y)
            return
        dx = event.x - self.drag_last_xy[0]
        dy = event.y - self.drag_last_xy[1]
        self.drag_last_xy = (event.x, event.y)
        self.view_yaw_deg += dx * 0.5
        self.view_pitch_deg = max(-85.0, min(85.0, self.view_pitch_deg - dy * 0.4))
        self.view_dirty = True
        self._refresh_trajectory(force=True)

    def _on_traj_release(self, _event) -> None:
        self.drag_last_xy = None

    def _on_traj_zoom(self, event) -> None:
        delta = getattr(event, "delta", 0)
        num = getattr(event, "num", None)
        if delta > 0 or num == 4:
            self.view_zoom *= 1.1
        elif delta < 0 or num == 5:
            self.view_zoom /= 1.1
        self.view_zoom = max(0.35, min(3.5, self.view_zoom))
        self.view_dirty = True
        self._refresh_trajectory(force=True)

    def _on_pose_filter_change(self) -> None:
        self.view_dirty = True
        self._refresh_trajectory(force=True)

    def run(self) -> None:
        self.spin_thread.start()
        self._tick()
        self.root.mainloop()

    def _tick(self) -> None:
        if not self.running:
            return
        self._refresh_images()
        self._refresh_trajectory()
        self.root.after(self.node.ui_refresh_ms, self._tick)

    def _refresh_images(self) -> None:
        now = time.monotonic()
        min_render_interval = 0.0 if self.node.display_fps_limit <= 0 else 1.0 / self.node.display_fps_limit
        for camera in self.node.cameras:
            panel = self.image_panels[camera.name]
            if self.node.latest_images[camera.name] is None:
                panel["stats"].configure(text="waiting")
            else:
                panel["stats"].configure(text=f"{self.image_display_fps[camera.name]:.1f} FPS")
            image = self.node.latest_images[camera.name]
            if image is None:
                continue
            canvas = panel["image"]
            canvas_w = max(canvas.winfo_width(), 10)
            canvas_h = max(canvas.winfo_height(), 10)
            panel_size = (canvas_w, canvas_h)
            if (
                self.last_image_versions[camera.name] == self.node.image_versions[camera.name]
                and self.last_panel_sizes[camera.name] == panel_size
            ):
                continue
            if min_render_interval > 0 and (now - self.last_image_render_time[camera.name]) < min_render_interval:
                continue

            rendered = self._fit_image(image, canvas_w, canvas_h)
            photo = ImageTk.PhotoImage(Image.fromarray(rendered))
            self.photo_refs[camera.name] = photo
            canvas.delete("all")
            canvas.create_image(canvas_w / 2, canvas_h / 2, image=photo, anchor="center")
            previous_render_time = self.last_image_render_time[camera.name]
            if previous_render_time > 0:
                inst_fps = 1.0 / max(now - previous_render_time, 1e-6)
                self.image_display_fps[camera.name] = (
                    inst_fps if self.image_display_fps[camera.name] <= 0
                    else 0.7 * self.image_display_fps[camera.name] + 0.3 * inst_fps
                )
            self.last_image_render_time[camera.name] = now
            self.last_image_versions[camera.name] = self.node.image_versions[camera.name]
            self.last_panel_sizes[camera.name] = panel_size

    def _fit_image(self, image: np.ndarray, max_width: int, max_height: int) -> np.ndarray:
        height, width = image.shape[:2]
        scale = min(max_width / width, max_height / height)
        new_w = max(1, int(width * scale))
        new_h = max(1, int(height * scale))
        return cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)

    def _refresh_trajectory(self, force: bool = False) -> None:
        visible_pose_names = [
            pose.name for pose in self.node.poses if self.pose_visibility_vars[pose.name].get()
        ]
        for pose in self.node.poses:
            self.pose_labels[pose.name].configure(text=self._pose_text(pose.name, pose.name in visible_pose_names))

        if not force and not self.view_dirty and all(
            self.last_pose_versions[pose.name] == self.node.pose_versions[pose.name]
            for pose in self.node.poses
        ):
            return

        self.traj_canvas.delete("all")
        width = max(self.traj_canvas.winfo_width(), 200)
        height = max(self.traj_canvas.winfo_height(), 200)

        all_points = [p for pose in self.node.poses if pose.name in visible_pose_names for p in self.node.traces[pose.name]]
        if not all_points:
            self.traj_canvas.create_text(
                width / 2,
                height / 2,
                text="Select one or more cameras to view trajectory.",
                fill=self.MUTED,
                font=("Helvetica", 16, "bold"),
            )
            return

        projected, _bounds, scene = self._project_traces(all_points, width=width, height=height)
        self._draw_axes(width, height, scene)

        for pose in self.node.poses:
            trace = self.node.traces[pose.name]
            if pose.name not in visible_pose_names or not trace:
                continue
            points_2d, _trace_bounds, _ = self._project_traces(trace, scene=scene, width=width, height=height)
            if len(points_2d) > 1:
                coords = [coord for point in points_2d for coord in point]
                self.traj_canvas.create_line(*coords, fill=pose.color, width=3)
            x, y = points_2d[-1]
            self.traj_canvas.create_oval(x - 5, y - 5, x + 5, y + 5, fill=pose.color, outline="")
            self.last_pose_versions[pose.name] = self.node.pose_versions[pose.name]
        self.view_dirty = False

    def _pose_text(self, name: str, is_visible: bool) -> str:
        pose = self.node.latest_pose[name]
        if not is_visible:
            return f"{name}: hidden"
        if pose is None:
            return f"{name}: waiting for VIO"
        return f"{name}: x={pose[0]:.2f}, y={pose[1]:.2f}, z={pose[2]:.2f}"

    def _project_traces(self, points: List[Tuple[float, float, float]], scene=None, width: int = 0, height: int = 0):
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
            screen_x = center_x + x1 * scale
            screen_y = center_y - y2 * scale
            projected.append((screen_x, screen_y))

        xs = [p[0] for p in projected]
        ys = [p[1] for p in projected]
        bounds = (min(xs), max(xs), min(ys), max(ys))
        return projected, bounds, scene

    def _draw_axes(self, width: int, height: int, scene) -> None:
        radius = max(scene["radius"], 0.5)
        axes = [
            ((0.0, 0.0, 0.0), (radius, 0.0, 0.0), "#ff6b6b", "x"),
            ((0.0, 0.0, 0.0), (0.0, radius, 0.0), "#5dade2", "y"),
            ((0.0, 0.0, 0.0), (0.0, 0.0, radius), "#58d68d", "z"),
        ]
        for start, end, color, label in axes:
            projected, _bounds, _ = self._project_traces([start, end], scene=scene, width=width, height=height)
            x1, y1 = projected[0]
            x2, y2 = projected[1]
            self.traj_canvas.create_line(x1, y1, x2, y2, fill=color, width=2)
            self.traj_canvas.create_text(x2 + 8, y2, anchor="w", text=label, fill=color, font=("Helvetica", 10, "bold"))

        self.traj_canvas.create_text(28, 22, anchor="nw", text=f"yaw {self.view_yaw_deg:.0f}°, pitch {self.view_pitch_deg:.0f}°, zoom {self.view_zoom:.2f}x", fill=self.MUTED, font=("Helvetica", 10))
        self.traj_canvas.create_text(
            width - 28,
            22,
            anchor="ne",
            text="drag rotate, wheel zoom",
            fill=self.MUTED,
            font=("Helvetica", 10),
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
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    app = DashboardApp(node, executor)
    try:
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        executor.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
