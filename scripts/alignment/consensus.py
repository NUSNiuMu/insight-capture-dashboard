"""Transform inlier selection and pose distance metrics."""

from __future__ import annotations

import math
from typing import List, Tuple

import numpy as np

from session_alignment import average_transforms


class AlignmentConsensus:
    def __init__(self, owner) -> None:
        self.owner = owner

    def _inlier_transform_indices(
        self,
        transforms: List[np.ndarray],
        translation_floor_m: float,
        rotation_floor_deg: float,
    ) -> List[int]:
        if len(transforms) < 3:
            return list(range(len(transforms)))
        consensus_center = average_transforms(transforms)
        if consensus_center is None:
            return []
        errors = [self.owner._pose_delta_metrics(consensus_center, transform) for transform in transforms]
        translation_errors = np.array([item[0] for item in errors], dtype=np.float64)
        rotation_errors = np.array([item[1] for item in errors], dtype=np.float64)
        translation_median = float(np.median(translation_errors))
        rotation_median = float(np.median(rotation_errors))
        translation_mad = float(np.median(np.abs(translation_errors - translation_median)))
        rotation_mad = float(np.median(np.abs(rotation_errors - rotation_median)))
        translation_gate = max(translation_floor_m, translation_median + 3.0 * max(translation_mad, 1e-4))
        rotation_gate = max(rotation_floor_deg, rotation_median + 3.0 * max(rotation_mad, 0.05))
        return [
            index
            for index, (translation_error, rotation_error) in enumerate(errors)
            if translation_error <= translation_gate and rotation_error <= rotation_gate
        ]

    @staticmethod
    def _pose_delta_metrics(reference: np.ndarray, candidate: np.ndarray) -> Tuple[float, float]:
        # Keep translation distance independent from rotation error.
        translation_norm_m = float(np.linalg.norm(reference[:3, 3] - candidate[:3, 3]))
        trace = float(np.trace(reference[:3, :3] @ candidate[:3, :3].T))
        cos_theta = max(-1.0, min(1.0, (trace - 1.0) * 0.5))
        rotation_angle_deg = math.degrees(math.acos(cos_theta))
        return translation_norm_m, rotation_angle_deg
