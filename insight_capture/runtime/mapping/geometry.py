"""稀疏双目建图使用的几何、标定和坐标变换工具。

本模块约定所有 4x4 矩阵均表示“把局部坐标中的点变换到目标坐标”的刚体变换。
例如 ``odom_to_left`` 表示左目坐标到 odom 坐标的变换。双目输入已经由相机节点
校正，因此三角化直接使用 CameraInfo 中的投影矩阵，不再次应用畸变参数。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np


@dataclass(frozen=True)
class PoseSample:
    """带纳秒时间戳的 ``T_world_body``，用于把 VIO 插值到图像时刻。"""

    stamp_ns: int
    translation: np.ndarray
    orientation_xyzw: np.ndarray


@dataclass(frozen=True)
class StereoCalibration:
    """已校正双目的左右投影矩阵、图像尺寸及可推导基线。"""

    left_projection: np.ndarray
    right_projection: np.ndarray
    width: int
    height: int

    @classmethod
    def from_camera_info(
        cls,
        left_projection: Sequence[float],
        right_projection: Sequence[float],
        width: int,
        height: int,
    ) -> "StereoCalibration":
        left = np.asarray(left_projection, dtype=np.float64).reshape(3, 4)
        right = np.asarray(right_projection, dtype=np.float64).reshape(3, 4)
        if width <= 0 or height <= 0:
            raise ValueError("stereo image dimensions must be positive")
        if left[0, 0] <= 0.0 or left[1, 1] <= 0.0:
            raise ValueError("left projection matrix has invalid focal length")
        if not np.isfinite(left).all() or not np.isfinite(right).all():
            raise ValueError("stereo projection matrices must be finite")
        baseline = abs(float(right[0, 3] / right[0, 0] - left[0, 3] / left[0, 0]))
        if baseline < 1e-4:
            raise ValueError("stereo projection matrices have a zero baseline")
        return cls(left, right, int(width), int(height))

    @property
    def baseline_m(self) -> float:
        left_x = self.left_projection[0, 3] / self.left_projection[0, 0]
        right_x = self.right_projection[0, 3] / self.right_projection[0, 0]
        return abs(float(right_x - left_x))


def _normalized_quaternion(value: Sequence[float]) -> np.ndarray:
    quaternion = np.asarray(value, dtype=np.float64).reshape(4)
    norm = float(np.linalg.norm(quaternion))
    if norm < 1e-12 or not np.isfinite(norm):
        raise ValueError("quaternion must be finite and non-zero")
    return quaternion / norm


def quaternion_slerp(
    first_xyzw: Sequence[float], second_xyzw: Sequence[float], fraction: float
) -> np.ndarray:
    """沿四元数最短弧插值，避免 ``q`` 与 ``-q`` 等价造成长路径旋转。"""

    first = _normalized_quaternion(first_xyzw)
    second = _normalized_quaternion(second_xyzw)
    dot = float(np.dot(first, second))
    if dot < 0.0:
        second = -second
        dot = -dot
    dot = float(np.clip(dot, -1.0, 1.0))
    fraction = float(np.clip(fraction, 0.0, 1.0))
    if dot > 0.9995:
        return _normalized_quaternion(first + fraction * (second - first))
    theta = float(np.arccos(dot))
    sin_theta = float(np.sin(theta))
    first_weight = np.sin((1.0 - fraction) * theta) / sin_theta
    second_weight = np.sin(fraction * theta) / sin_theta
    return _normalized_quaternion(first_weight * first + second_weight * second)


def interpolate_pose(first: PoseSample, second: PoseSample, stamp_ns: int) -> PoseSample:
    """在两个 VIO 样本间对平移线性插值、对旋转球面插值。"""

    if second.stamp_ns <= first.stamp_ns:
        raise ValueError("pose interpolation requires increasing timestamps")
    fraction = (int(stamp_ns) - first.stamp_ns) / (second.stamp_ns - first.stamp_ns)
    if fraction < 0.0 or fraction > 1.0:
        raise ValueError("interpolation timestamp is outside the pose interval")
    translation = first.translation + fraction * (second.translation - first.translation)
    orientation = quaternion_slerp(
        first.orientation_xyzw, second.orientation_xyzw, fraction
    )
    return PoseSample(int(stamp_ns), translation, orientation)


def matrix_from_pose(pose: PoseSample) -> np.ndarray:
    """Return the homogeneous ``T_world_body`` matrix for a pose sample."""

    quaternion = _normalized_quaternion(pose.orientation_xyzw)
    x, y, z, w = quaternion
    rotation = np.array(
        [
            [1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - z * w), 2.0 * (x * z + y * w)],
            [2.0 * (x * y + z * w), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - x * w)],
            [2.0 * (x * z - y * w), 2.0 * (y * z + x * w), 1.0 - 2.0 * (x * x + y * y)],
        ],
        dtype=np.float64,
    )
    transform = np.eye(4, dtype=np.float64)
    transform[:3, :3] = rotation
    transform[:3, 3] = np.asarray(pose.translation, dtype=np.float64).reshape(3)
    return transform


def matrix_from_transform(
    translation: Sequence[float], orientation_xyzw: Sequence[float]
) -> np.ndarray:
    """Return ``T_parent_child`` from a ROS Transform payload."""

    pose = PoseSample(
        stamp_ns=0,
        translation=np.asarray(translation, dtype=np.float64),
        orientation_xyzw=np.asarray(orientation_xyzw, dtype=np.float64),
    )
    return matrix_from_pose(pose)


def compose_transform(first: np.ndarray, second: np.ndarray) -> np.ndarray:
    """组合 ``T_a_b`` 与 ``T_b_c`` 得到 ``T_a_c``。"""

    first = np.asarray(first, dtype=np.float64).reshape(4, 4)
    second = np.asarray(second, dtype=np.float64).reshape(4, 4)
    return first @ second


def left_to_stereo_center(left_to_right: np.ndarray) -> np.ndarray:
    """返回左目到双目光心中点的 ``T_left_center``。

    中点坐标系保持左目姿态，只把原点放到两个光心之间。这样不会把右目标定中的微小
    旋转误差引入设备发布姿态。
    """

    transform = np.asarray(left_to_right, dtype=np.float64).reshape(4, 4)
    if not np.all(np.isfinite(transform)):
        raise ValueError("left-to-right transform must be finite")
    if not np.allclose(transform[3], (0.0, 0.0, 0.0, 1.0), atol=1e-9):
        raise ValueError("left-to-right transform is not homogeneous")
    center = np.eye(4, dtype=np.float64)
    center[:3, 3] = 0.5 * transform[:3, 3]
    return center


def transform_points(transform: np.ndarray, points: np.ndarray) -> np.ndarray:
    """Transform an ``N x 3`` point array without homogeneous allocation."""

    transform = np.asarray(transform, dtype=np.float64).reshape(4, 4)
    points = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    return points @ transform[:3, :3].T + transform[:3, 3]


def rotation_distance_deg(first: np.ndarray, second: np.ndarray) -> float:
    """Return the geodesic rotation distance between two transforms."""

    relative = np.asarray(first, dtype=np.float64)[:3, :3].T @ np.asarray(
        second, dtype=np.float64
    )[:3, :3]
    cosine = float(np.clip((np.trace(relative) - 1.0) * 0.5, -1.0, 1.0))
    return float(np.degrees(np.arccos(cosine)))
