import math
from typing import List, Tuple

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets


class ImagePanel(QtWidgets.QFrame):
    class AspectRatioLabel(QtWidgets.QLabel):
        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self.aspect_ratio = 16.0 / 9.0
            self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)

        def set_aspect_ratio(self, width: int, height: int) -> None:
            if width > 0 and height > 0:
                ratio = float(width) / float(height)
                if abs(ratio - self.aspect_ratio) > 1e-3:
                    self.aspect_ratio = ratio
                    self.updateGeometry()

        def hasHeightForWidth(self) -> bool:
            return True

        def heightForWidth(self, width: int) -> int:
            if self.aspect_ratio <= 1e-6:
                return max(width, 1)
            return max(int(round(width / self.aspect_ratio)), 1)

        def sizeHint(self) -> QtCore.QSize:
            width = 480
            return QtCore.QSize(width, self.heightForWidth(width))

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.setStyleSheet(
            "QFrame { background: #16202a; border: 1px solid #233142; border-radius: 6px; }"
            "QLabel { color: #e9eef4; }"
        )
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        self.title_label = QtWidgets.QLabel(title)
        self.title_label.setStyleSheet("font-size: 16px; font-weight: 600; color: #e9eef4; border: none;")
        layout.addWidget(self.title_label)

        self.fps_label = QtWidgets.QLabel("waiting")
        self.fps_label.setStyleSheet("font-size: 12px; color: #8fa3b8; border: none;")
        layout.addWidget(self.fps_label)

        self.image_label = self.AspectRatioLabel()
        self.image_label.setAlignment(QtCore.Qt.AlignCenter)
        self.image_label.setMinimumSize(180, 120)
        self.image_label.setStyleSheet("background: #0c1116; border: none;")
        self.image_label.setText("Waiting for image...")
        layout.addWidget(self.image_label, 1)

    def set_image_shape(self, width: int, height: int) -> None:
        self.image_label.set_aspect_ratio(width, height)


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


class DashboardTrajectoryMixin:
    MUTED = "#8fa3b8"

    def _pixmap_from_image(self, image: np.ndarray, target_size: QtCore.QSize) -> QtGui.QPixmap:
        image = np.ascontiguousarray(image, dtype=np.uint8)
        h, w = image.shape[:2]
        bytes_per_line = w * 3
        qimage = QtGui.QImage(image.data, w, h, bytes_per_line, QtGui.QImage.Format_RGB888)
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
        trace_map = {
            pose.name: self.node.transformed_trace(pose.name)
            for pose in self.node.poses
            if pose.name in visible_pose_names
        }
        all_points = [p for pose_name in visible_pose_names for p in trace_map.get(pose_name, [])]
        if not all_points:
            painter.setPen(QtGui.QColor(self.MUTED))
            painter.drawText(self.rect_for(width, height), QtCore.Qt.AlignCenter, "Select one or more cameras to view trajectory.")
            return

        _, _, scene = self._project_traces(all_points, width, height)
        self._draw_axes(painter, width, height, scene)
        for pose in self.node.poses:
            if pose.name not in visible_pose_names:
                continue
            trace = trace_map.get(pose.name, [])
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
            # Z-up convention: yaw rotates in the x/y ground plane, and pitch
            # tilts the camera against depth while keeping z as the vertical axis.
            x1 = cos_y * x - sin_y * y
            depth1 = sin_y * x + cos_y * y
            z1 = z
            z2 = cos_p * z1 - sin_p * depth1
            depth2 = sin_p * z1 + cos_p * depth1
            rotated.append((x1, z2, depth2))
            radius = max(radius, abs(x1), abs(z2), abs(depth2))

        if scene is None:
            scene = {"center": (cx, cy, cz), "radius": radius}
        else:
            radius = max(scene["radius"], 0.5)

        center_x = width / 2
        center_y = height / 2
        focal = min(usable_w, usable_h) * 0.95 * self.view_zoom
        camera_distance = radius * 4.0
        projected = []
        for x1, z2, depth2 in rotated:
            denom = max(camera_distance - depth2, radius * 0.3)
            scale = focal / denom
            projected.append((center_x + x1 * scale, center_y - z2 * scale))

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
