"""Coordinate transforms, VIO interpolation, and rectified stereo geometry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

import cv2
import numpy as np


@dataclass(frozen=True)
class PoseSample:
    """A timestamped ``T_world_body`` pose."""

    stamp_ns: int
    translation: np.ndarray
    orientation_xyzw: np.ndarray


@dataclass(frozen=True)
class StereoCalibration:
    """Projection matrices for an already-rectified stereo pair."""

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


@dataclass(frozen=True)
class TriangulationResult:
    """Geometrically valid left-camera points and their source match indices."""

    points_left: np.ndarray
    source_indices: np.ndarray
    reprojection_error_px: np.ndarray
    disparity_px: np.ndarray


def _normalized_quaternion(value: Sequence[float]) -> np.ndarray:
    quaternion = np.asarray(value, dtype=np.float64).reshape(4)
    norm = float(np.linalg.norm(quaternion))
    if norm < 1e-12 or not np.isfinite(norm):
        raise ValueError("quaternion must be finite and non-zero")
    return quaternion / norm


def quaternion_slerp(
    first_xyzw: Sequence[float], second_xyzw: Sequence[float], fraction: float
) -> np.ndarray:
    """Interpolate unit quaternions along their shortest arc."""

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
    """Linearly interpolate translation and slerp orientation."""

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
    """Compose ``T_a_b`` and ``T_b_c`` into ``T_a_c``."""

    first = np.asarray(first, dtype=np.float64).reshape(4, 4)
    second = np.asarray(second, dtype=np.float64).reshape(4, 4)
    return first @ second


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


def triangulate_rectified(
    left_points: np.ndarray,
    right_points: np.ndarray,
    calibration: StereoCalibration,
    *,
    max_epipolar_error_px: float = 1.5,
    min_disparity_px: float = 1.0,
    max_disparity_px: float = 320.0,
    min_depth_m: float = 0.20,
    max_depth_m: float = 8.0,
    max_reprojection_error_px: float = 1.5,
) -> TriangulationResult:
    """Triangulate matched points and reject physically inconsistent results."""

    left = np.asarray(left_points, dtype=np.float64).reshape(-1, 2)
    right = np.asarray(right_points, dtype=np.float64).reshape(-1, 2)
    if left.shape != right.shape:
        raise ValueError("left and right match arrays must have the same shape")
    if left.size == 0:
        return TriangulationResult(
            np.empty((0, 3), dtype=np.float64),
            np.empty((0,), dtype=np.int64),
            np.empty((0,), dtype=np.float64),
            np.empty((0,), dtype=np.float64),
        )

    disparity = left[:, 0] - right[:, 0]
    preliminary = (
        np.isfinite(left).all(axis=1)
        & np.isfinite(right).all(axis=1)
        & (np.abs(left[:, 1] - right[:, 1]) <= float(max_epipolar_error_px))
        & (disparity >= float(min_disparity_px))
        & (disparity <= float(max_disparity_px))
    )
    source_indices = np.flatnonzero(preliminary)
    if source_indices.size == 0:
        return TriangulationResult(
            np.empty((0, 3), dtype=np.float64),
            source_indices.astype(np.int64),
            np.empty((0,), dtype=np.float64),
            np.empty((0,), dtype=np.float64),
        )

    filtered_left = left[source_indices]
    filtered_right = right[source_indices]
    homogeneous = cv2.triangulatePoints(
        calibration.left_projection,
        calibration.right_projection,
        filtered_left.T,
        filtered_right.T,
    ).T
    valid_w = np.abs(homogeneous[:, 3]) > 1e-12
    points = np.full((len(homogeneous), 3), np.nan, dtype=np.float64)
    points[valid_w] = homogeneous[valid_w, :3] / homogeneous[valid_w, 3:4]

    ones = np.ones((len(points), 1), dtype=np.float64)
    points_h = np.concatenate([points, ones], axis=1)
    left_projected = points_h @ calibration.left_projection.T
    right_projected = points_h @ calibration.right_projection.T
    projection_valid = (
        np.abs(left_projected[:, 2]) > 1e-12
    ) & (np.abs(right_projected[:, 2]) > 1e-12)
    left_uv = np.full_like(filtered_left, np.nan)
    right_uv = np.full_like(filtered_right, np.nan)
    left_uv[projection_valid] = (
        left_projected[projection_valid, :2]
        / left_projected[projection_valid, 2:3]
    )
    right_uv[projection_valid] = (
        right_projected[projection_valid, :2]
        / right_projected[projection_valid, 2:3]
    )
    reprojection = np.maximum(
        np.linalg.norm(left_uv - filtered_left, axis=1),
        np.linalg.norm(right_uv - filtered_right, axis=1),
    )
    depth = points[:, 2]
    valid = (
        valid_w
        & projection_valid
        & np.isfinite(points).all(axis=1)
        & (depth >= float(min_depth_m))
        & (depth <= float(max_depth_m))
        & (reprojection <= float(max_reprojection_error_px))
    )
    return TriangulationResult(
        points_left=points[valid],
        source_indices=source_indices[valid].astype(np.int64),
        reprojection_error_px=reprojection[valid],
        disparity_px=disparity[source_indices[valid]],
    )
