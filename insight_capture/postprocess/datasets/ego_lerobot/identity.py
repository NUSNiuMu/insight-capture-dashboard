"""Physical-hand identity gates shared by all model backends."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class IdentityCorrection:
    frame_index: int
    source_hand: int
    target_hand: int
    same_label_step_m: float
    opposite_label_step_m: float


def projection_outliers(
    keypoints_2d: np.ndarray,
    detected: np.ndarray,
    projected_wrist_pixels: np.ndarray,
    projection_valid: np.ndarray,
    *,
    maximum_distance_px: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Return per-hand rejection mask and measured wrist-pixel distances."""

    distance = np.linalg.norm(
        keypoints_2d[:, :, 0] - projected_wrist_pixels, axis=2
    )
    measurable = detected & projection_valid & np.isfinite(distance)
    return measurable & (distance > maximum_distance_px), distance


def temporal_identity_corrections(
    wrist_positions: np.ndarray,
    valid: np.ndarray,
    *,
    minimum_same_label_step_m: float = 0.15,
    minimum_advantage_m: float = 0.08,
) -> list[IdentityCorrection]:
    """Find isolated one-hand labels that match the opposite prior track."""

    corrections = []
    for frame in range(1, len(wrist_positions)):
        current_valid = np.flatnonzero(valid[frame])
        if len(current_valid) != 1 or not np.all(valid[frame - 1]):
            continue
        source = int(current_valid[0])
        target = 1 - source
        current = wrist_positions[frame, source]
        same = float(np.linalg.norm(current - wrist_positions[frame - 1, source]))
        opposite = float(np.linalg.norm(current - wrist_positions[frame - 1, target]))
        if (
            same > minimum_same_label_step_m
            and opposite + minimum_advantage_m < same
        ):
            corrections.append(
                IdentityCorrection(frame, source, target, same, opposite)
            )
    return corrections


def move_hand_slot(values: np.ndarray, correction: IdentityCorrection) -> None:
    """Move one frame from a wrong hand slot into an empty physical slot."""

    frame = correction.frame_index
    values[frame, correction.target_hand] = values[frame, correction.source_hand]
    values[frame, correction.source_hand] = 0
