"""Geometry and quality helpers for offline multi-view hand pose extraction."""

from __future__ import annotations

from bisect import bisect_left
import math
from typing import Optional, Sequence

import numpy as np


KEYPOINT_NAMES = (
    "wrist",
    "thumb_1",
    "thumb_2",
    "thumb_3",
    "thumb_tip",
    "index_mcp",
    "index_pip",
    "index_dip",
    "index_tip",
    "middle_mcp",
    "middle_pip",
    "middle_dip",
    "middle_tip",
    "ring_mcp",
    "ring_pip",
    "ring_dip",
    "ring_tip",
    "pinky_mcp",
    "pinky_pip",
    "pinky_dip",
    "pinky_tip",
)

MANO_JOINT_ROTATION_NAMES = (
    "index_mcp",
    "index_pip",
    "index_dip",
    "middle_mcp",
    "middle_pip",
    "middle_dip",
    "pinky_mcp",
    "pinky_pip",
    "pinky_dip",
    "ring_mcp",
    "ring_pip",
    "ring_dip",
    "thumb_cmc",
    "thumb_mcp",
    "thumb_ip",
)

HAND_CONNECTIONS = (
    (0, 1),
    (1, 2),
    (2, 3),
    (3, 4),
    (0, 5),
    (5, 6),
    (6, 7),
    (7, 8),
    (5, 9),
    (9, 10),
    (10, 11),
    (11, 12),
    (9, 13),
    (13, 14),
    (14, 15),
    (15, 16),
    (13, 17),
    (17, 18),
    (18, 19),
    (19, 20),
    (0, 17),
)


def normalize_quaternion(quaternion_xyzw: Sequence[float]) -> np.ndarray:
    value = np.asarray(quaternion_xyzw, dtype=np.float64)
    norm = float(np.linalg.norm(value))
    if value.shape != (4,) or norm < 1e-12 or not np.isfinite(norm):
        raise ValueError("invalid quaternion")
    return value / norm


def rotation_from_quaternion(quaternion_xyzw: Sequence[float]) -> np.ndarray:
    x, y, z, w = normalize_quaternion(quaternion_xyzw)
    return np.asarray(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ],
        dtype=np.float64,
    )


def quaternion_from_rotation(rotation: np.ndarray) -> np.ndarray:
    matrix = np.asarray(rotation, dtype=np.float64)
    if matrix.shape != (3, 3):
        raise ValueError("rotation must be 3x3")
    trace = float(np.trace(matrix))
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        quaternion = np.asarray(
            [
                (matrix[2, 1] - matrix[1, 2]) / scale,
                (matrix[0, 2] - matrix[2, 0]) / scale,
                (matrix[1, 0] - matrix[0, 1]) / scale,
                0.25 * scale,
            ]
        )
    else:
        diagonal = int(np.argmax(np.diag(matrix)))
        if diagonal == 0:
            scale = math.sqrt(1.0 + matrix[0, 0] - matrix[1, 1] - matrix[2, 2]) * 2.0
            quaternion = np.asarray(
                [
                    0.25 * scale,
                    (matrix[0, 1] + matrix[1, 0]) / scale,
                    (matrix[0, 2] + matrix[2, 0]) / scale,
                    (matrix[2, 1] - matrix[1, 2]) / scale,
                ]
            )
        elif diagonal == 1:
            scale = math.sqrt(1.0 + matrix[1, 1] - matrix[0, 0] - matrix[2, 2]) * 2.0
            quaternion = np.asarray(
                [
                    (matrix[0, 1] + matrix[1, 0]) / scale,
                    0.25 * scale,
                    (matrix[1, 2] + matrix[2, 1]) / scale,
                    (matrix[0, 2] - matrix[2, 0]) / scale,
                ]
            )
        else:
            scale = math.sqrt(1.0 + matrix[2, 2] - matrix[0, 0] - matrix[1, 1]) * 2.0
            quaternion = np.asarray(
                [
                    (matrix[0, 2] + matrix[2, 0]) / scale,
                    (matrix[1, 2] + matrix[2, 1]) / scale,
                    0.25 * scale,
                    (matrix[1, 0] - matrix[0, 1]) / scale,
                ]
            )
    return normalize_quaternion(quaternion)


def matrix_from_pose(
    position: Sequence[float], quaternion_xyzw: Sequence[float]
) -> np.ndarray:
    transform = np.eye(4, dtype=np.float64)
    transform[:3, :3] = rotation_from_quaternion(quaternion_xyzw)
    transform[:3, 3] = np.asarray(position, dtype=np.float64)
    return transform


def pose_from_matrix(transform: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    matrix = np.asarray(transform, dtype=np.float64)
    if matrix.shape != (4, 4):
        raise ValueError("transform must be 4x4")
    return matrix[:3, 3].copy(), quaternion_from_rotation(matrix[:3, :3])


def quaternion_slerp(
    first_xyzw: Sequence[float], second_xyzw: Sequence[float], ratio: float
) -> np.ndarray:
    first = normalize_quaternion(first_xyzw)
    second = normalize_quaternion(second_xyzw)
    dot = float(np.dot(first, second))
    if dot < 0.0:
        second = -second
        dot = -dot
    dot = min(1.0, max(-1.0, dot))
    if dot > 0.9995:
        return normalize_quaternion(first + ratio * (second - first))
    angle = math.acos(dot)
    sin_angle = math.sin(angle)
    return (
        math.sin((1.0 - ratio) * angle) / sin_angle * first
        + math.sin(ratio * angle) / sin_angle * second
    )


def interpolate_pose(
    samples: Sequence[tuple[int, np.ndarray, np.ndarray]],
    stamp_ns: int,
    *,
    max_bracket_gap_ns: int,
) -> Optional[dict]:
    """Interpolate one T_map_camera_center sample around a requested stamp."""
    if not samples or stamp_ns < samples[0][0] or stamp_ns > samples[-1][0]:
        return None
    stamps = [sample[0] for sample in samples]
    right = bisect_left(stamps, stamp_ns)
    if right < len(samples) and samples[right][0] == stamp_ns:
        sample = samples[right]
        return {
            "transform": matrix_from_pose(sample[1], sample[2]),
            "nearest_gap_ns": 0,
            "bracket_gap_ns": 0,
        }
    if right == 0 or right >= len(samples):
        return None
    first = samples[right - 1]
    second = samples[right]
    bracket_gap_ns = second[0] - first[0]
    if bracket_gap_ns <= 0 or bracket_gap_ns > max_bracket_gap_ns:
        return None
    ratio = (stamp_ns - first[0]) / bracket_gap_ns
    position = first[1] + ratio * (second[1] - first[1])
    quaternion = quaternion_slerp(first[2], second[2], ratio)
    return {
        "transform": matrix_from_pose(position, quaternion),
        "nearest_gap_ns": min(stamp_ns - first[0], second[0] - stamp_ns),
        "bracket_gap_ns": bracket_gap_ns,
    }


def transform_points(transform: np.ndarray, points: np.ndarray) -> np.ndarray:
    values = np.asarray(points, dtype=np.float64)
    if values.ndim != 2 or values.shape[1] != 3:
        raise ValueError("points must be Nx3")
    return (values @ transform[:3, :3].T) + transform[:3, 3]


def project_points(points_camera: np.ndarray, intrinsic: np.ndarray) -> np.ndarray:
    values = np.asarray(points_camera, dtype=np.float64)
    matrix = np.asarray(intrinsic, dtype=np.float64)
    if values.ndim != 2 or values.shape[1] != 3 or matrix.shape != (3, 3):
        raise ValueError("invalid points or intrinsic matrix")
    projected = np.full((len(values), 2), np.nan, dtype=np.float64)
    valid = values[:, 2] > 1e-6
    normalized = values[valid, :2] / values[valid, 2:3]
    projected[valid, 0] = matrix[0, 0] * normalized[:, 0] + matrix[0, 2]
    projected[valid, 1] = matrix[1, 1] * normalized[:, 1] + matrix[1, 2]
    return projected


def numeric_summary(values: Sequence[float]) -> Optional[dict]:
    array = np.asarray(values, dtype=np.float64)
    array = array[np.isfinite(array)]
    if not len(array):
        return None
    return {
        "count": int(len(array)),
        "min": round(float(np.min(array)), 6),
        "p50": round(float(np.percentile(array, 50)), 6),
        "p95": round(float(np.percentile(array, 95)), 6),
        "max": round(float(np.max(array)), 6),
        "mean": round(float(np.mean(array)), 6),
        "std": round(float(np.std(array)), 6),
    }


def json_ready(value):
    """Replace non-finite numeric sentinels with JSON null recursively."""
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    if isinstance(value, (float, np.floating)) and not math.isfinite(float(value)):
        return None
    if isinstance(value, np.integer):
        return int(value)
    return value


def bbox_iou(first: Sequence[float], second: Sequence[float]) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    first_area = max(0.0, first[2] - first[0]) * max(0.0, first[3] - first[1])
    second_area = max(0.0, second[2] - second[0]) * max(0.0, second[3] - second[1])
    union = first_area + second_area - intersection
    return intersection / union if union > 0.0 else 0.0


def suppress_overlapping_hands(
    candidates: list[dict], threshold: float = 0.65
) -> tuple[list[dict], int]:
    """Suppress class-specific detector duplicates over one physical hand."""
    kept = []
    discarded = 0
    for candidate in sorted(candidates, key=lambda item: item["confidence"], reverse=True):
        if any(
            bbox_iou(candidate["bbox_xyxy_px"], existing["bbox_xyxy_px"])
            >= threshold
            for existing in kept
        ):
            discarded += 1
            continue
        kept.append(candidate)
    return kept, discarded


def nearest_record(records: Sequence[dict], stamp_ns: int) -> Optional[dict]:
    if not records:
        return None
    stamps = [int(record["stamp_ns"]) for record in records]
    index = bisect_left(stamps, stamp_ns)
    candidates = []
    if index < len(records):
        candidates.append(records[index])
    if index > 0:
        candidates.append(records[index - 1])
    return min(candidates, key=lambda item: abs(int(item["stamp_ns"]) - stamp_ns))


def center_to_left_from_baseline(left_to_right: np.ndarray) -> np.ndarray:
    """Return T_camera_center_left for a midpoint center aligned to left optical."""
    transform = np.eye(4, dtype=np.float64)
    transform[:3, 3] = -0.5 * np.asarray(left_to_right[:3, 3], dtype=np.float64)
    return transform
