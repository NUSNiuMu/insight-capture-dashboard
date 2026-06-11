#!/usr/bin/env python3

import math
from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

import numpy as np


def quaternion_to_matrix(x: float, y: float, z: float, w: float) -> np.ndarray:
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    if norm <= 1e-12:
        return np.eye(3, dtype=np.float64)
    x /= norm
    y /= norm
    z /= norm
    w /= norm
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    return np.array(
        [
            [1.0 - 2.0 * (yy + zz), 2.0 * (xy - wz), 2.0 * (xz + wy)],
            [2.0 * (xy + wz), 1.0 - 2.0 * (xx + zz), 2.0 * (yz - wx)],
            [2.0 * (xz - wy), 2.0 * (yz + wx), 1.0 - 2.0 * (xx + yy)],
        ],
        dtype=np.float64,
    )


def matrix_to_transform(rotation: np.ndarray, translation: np.ndarray) -> np.ndarray:
    transform = np.eye(4, dtype=np.float64)
    transform[:3, :3] = rotation
    transform[:3, 3] = translation.reshape(3)
    return transform


def matrix_to_quaternion(rotation: np.ndarray) -> Tuple[float, float, float, float]:
    trace = float(rotation[0, 0] + rotation[1, 1] + rotation[2, 2])
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        w = 0.25 * s
        x = (rotation[2, 1] - rotation[1, 2]) / s
        y = (rotation[0, 2] - rotation[2, 0]) / s
        z = (rotation[1, 0] - rotation[0, 1]) / s
    elif rotation[0, 0] > rotation[1, 1] and rotation[0, 0] > rotation[2, 2]:
        s = math.sqrt(1.0 + rotation[0, 0] - rotation[1, 1] - rotation[2, 2]) * 2.0
        w = (rotation[2, 1] - rotation[1, 2]) / s
        x = 0.25 * s
        y = (rotation[0, 1] + rotation[1, 0]) / s
        z = (rotation[0, 2] + rotation[2, 0]) / s
    elif rotation[1, 1] > rotation[2, 2]:
        s = math.sqrt(1.0 + rotation[1, 1] - rotation[0, 0] - rotation[2, 2]) * 2.0
        w = (rotation[0, 2] - rotation[2, 0]) / s
        x = (rotation[0, 1] + rotation[1, 0]) / s
        y = 0.25 * s
        z = (rotation[1, 2] + rotation[2, 1]) / s
    else:
        s = math.sqrt(1.0 + rotation[2, 2] - rotation[0, 0] - rotation[1, 1]) * 2.0
        w = (rotation[1, 0] - rotation[0, 1]) / s
        x = (rotation[0, 2] + rotation[2, 0]) / s
        y = (rotation[1, 2] + rotation[2, 1]) / s
        z = 0.25 * s
    return normalize_quaternion((x, y, z, w))


def invert_transform(transform: np.ndarray) -> np.ndarray:
    rotation = transform[:3, :3]
    translation = transform[:3, 3]
    inv = np.eye(4, dtype=np.float64)
    inv[:3, :3] = rotation.T
    inv[:3, 3] = -rotation.T @ translation
    return inv


def transform_point(transform: np.ndarray, point: Tuple[float, float, float]) -> Tuple[float, float, float]:
    vec = np.array([point[0], point[1], point[2], 1.0], dtype=np.float64)
    mapped = transform @ vec
    return (float(mapped[0]), float(mapped[1]), float(mapped[2]))


def normalize_quaternion(quaternion_xyzw: Tuple[float, float, float, float]) -> Tuple[float, float, float, float]:
    quat = np.array(quaternion_xyzw, dtype=np.float64)
    norm = float(np.linalg.norm(quat))
    if norm <= 1e-12:
        return (0.0, 0.0, 0.0, 1.0)
    quat /= norm
    return (float(quat[0]), float(quat[1]), float(quat[2]), float(quat[3]))


def slerp_quaternion(
    start_xyzw: Tuple[float, float, float, float],
    end_xyzw: Tuple[float, float, float, float],
    alpha: float,
) -> Tuple[float, float, float, float]:
    start = np.array(normalize_quaternion(start_xyzw), dtype=np.float64)
    end = np.array(normalize_quaternion(end_xyzw), dtype=np.float64)
    dot = float(np.dot(start, end))
    if dot < 0.0:
        end = -end
        dot = -dot
    alpha = max(0.0, min(1.0, float(alpha)))
    if dot > 0.9995:
        return normalize_quaternion(tuple(start + alpha * (end - start)))
    theta_0 = math.acos(max(-1.0, min(1.0, dot)))
    sin_theta_0 = math.sin(theta_0)
    theta = theta_0 * alpha
    sin_theta = math.sin(theta)
    scale_start = math.cos(theta) - dot * sin_theta / sin_theta_0
    scale_end = sin_theta / sin_theta_0
    return normalize_quaternion(tuple(scale_start * start + scale_end * end))


@dataclass
class PoseSample:
    stamp_ns: int
    position: Tuple[float, float, float]
    orientation_xyzw: Tuple[float, float, float, float]

    def as_transform(self) -> np.ndarray:
        rotation = quaternion_to_matrix(*self.orientation_xyzw)
        translation = np.array(self.position, dtype=np.float64)
        return matrix_to_transform(rotation, translation)

    @classmethod
    def from_transform(cls, stamp_ns: int, transform: np.ndarray) -> "PoseSample":
        translation = transform[:3, 3]
        quaternion = matrix_to_quaternion(transform[:3, :3])
        return cls(
            stamp_ns=stamp_ns,
            position=(float(translation[0]), float(translation[1]), float(translation[2])),
            orientation_xyzw=quaternion,
        )


def interpolate_pose_sample(before: PoseSample, after: PoseSample, stamp_ns: int) -> PoseSample:
    if after.stamp_ns == before.stamp_ns:
        return before
    alpha = (stamp_ns - before.stamp_ns) / float(after.stamp_ns - before.stamp_ns)
    alpha = max(0.0, min(1.0, alpha))
    before_position = np.array(before.position, dtype=np.float64)
    after_position = np.array(after.position, dtype=np.float64)
    position = before_position + alpha * (after_position - before_position)
    orientation = slerp_quaternion(before.orientation_xyzw, after.orientation_xyzw, alpha)
    return PoseSample(
        stamp_ns=stamp_ns,
        position=(float(position[0]), float(position[1]), float(position[2])),
        orientation_xyzw=orientation,
    )


def average_transforms(transforms: Iterable[np.ndarray]) -> Optional[np.ndarray]:
    matrices = list(transforms)
    if not matrices:
        return None
    translations = np.array([matrix[:3, 3] for matrix in matrices], dtype=np.float64)
    mean_translation = translations.mean(axis=0)

    rotations = np.array([matrix[:3, :3] for matrix in matrices], dtype=np.float64)
    mean_rotation = rotations.mean(axis=0)
    u, _, vh = np.linalg.svd(mean_rotation)
    orthogonal_rotation = u @ vh
    if np.linalg.det(orthogonal_rotation) < 0:
        u[:, -1] *= -1.0
        orthogonal_rotation = u @ vh
    return matrix_to_transform(orthogonal_rotation, mean_translation)


def transform_pose_sample(transform: np.ndarray, pose_sample: PoseSample) -> PoseSample:
    mapped = transform @ pose_sample.as_transform()
    return PoseSample.from_transform(pose_sample.stamp_ns, mapped)
