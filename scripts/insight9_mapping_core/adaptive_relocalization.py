"""Select immediate or smoothed correction from relocalization magnitude."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .geometry import rotation_distance_deg
from .relocalization_ekf import RelocalizationEkf


@dataclass(frozen=True)
class AdaptiveRelocalizationConfig:
    """Thresholds above which a confirmed global correction is applied at once."""

    jump_translation_m: float = 0.50
    jump_rotation_deg: float = 25.0

    def __post_init__(self) -> None:
        if (
            not np.isfinite(self.jump_translation_m)
            or self.jump_translation_m <= 0.0
            or not np.isfinite(self.jump_rotation_deg)
            or self.jump_rotation_deg <= 0.0
        ):
            raise ValueError("adaptive relocalization thresholds must be positive")


@dataclass(frozen=True)
class AdaptiveRelocalizationUpdate:
    """Result of applying one confirmed absolute correction."""

    mode: str
    translation_m: float
    rotation_deg: float


class AdaptiveRelocalizationPolicy:
    """Jump after gross drift and retain EKF smoothing for small corrections."""

    def __init__(self, config: AdaptiveRelocalizationConfig) -> None:
        self.config = config

    def apply(
        self, pose_filter: RelocalizationEkf, measurement: np.ndarray
    ) -> AdaptiveRelocalizationUpdate:
        value = np.asarray(measurement, dtype=np.float64).reshape(4, 4)
        current = pose_filter.correction
        if current is None:
            pose_filter.observe(value)
            return AdaptiveRelocalizationUpdate("initialize", 0.0, 0.0)

        translation_m = float(np.linalg.norm(value[:3, 3] - current[:3, 3]))
        rotation_deg = rotation_distance_deg(current, value)
        if (
            translation_m >= self.config.jump_translation_m
            or rotation_deg >= self.config.jump_rotation_deg
        ):
            pose_filter.reinitialize(value)
            mode = "jump"
        else:
            pose_filter.observe(value)
            mode = "ekf"
        return AdaptiveRelocalizationUpdate(mode, translation_m, rotation_deg)
