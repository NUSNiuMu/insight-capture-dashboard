"""Keep a VIO trajectory continuous across isolated coordinate-frame resets."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np

from .geometry import PoseSample, matrix_from_pose, rotation_distance_deg


def _so3_exp(value: np.ndarray) -> np.ndarray:
    vector = np.asarray(value, dtype=np.float64).reshape(3)
    angle = float(np.linalg.norm(vector))
    if angle < 1e-12:
        skew = np.array(
            [
                [0.0, -vector[2], vector[1]],
                [vector[2], 0.0, -vector[0]],
                [-vector[1], vector[0], 0.0],
            ],
            dtype=np.float64,
        )
        return np.eye(3, dtype=np.float64) + skew
    axis = vector / angle
    skew = np.array(
        [
            [0.0, -axis[2], axis[1]],
            [axis[2], 0.0, -axis[0]],
            [-axis[1], axis[0], 0.0],
        ],
        dtype=np.float64,
    )
    return (
        np.eye(3, dtype=np.float64)
        + math.sin(angle) * skew
        + (1.0 - math.cos(angle)) * (skew @ skew)
    )


def _so3_log(rotation: np.ndarray) -> np.ndarray:
    value = np.asarray(rotation, dtype=np.float64).reshape(3, 3)
    cosine = float(np.clip((np.trace(value) - 1.0) * 0.5, -1.0, 1.0))
    angle = math.acos(cosine)
    if angle < 1e-12:
        return 0.5 * np.array(
            [
                value[2, 1] - value[1, 2],
                value[0, 2] - value[2, 0],
                value[1, 0] - value[0, 1],
            ],
            dtype=np.float64,
        )
    if math.pi - angle < 1e-5:
        eigenvalues, eigenvectors = np.linalg.eig(value)
        index = int(np.argmin(np.abs(eigenvalues - 1.0)))
        axis = np.real(eigenvectors[:, index]).astype(np.float64)
        norm = float(np.linalg.norm(axis))
        if norm < 1e-12:
            return np.zeros(3, dtype=np.float64)
        return angle * axis / norm
    scale = angle / (2.0 * math.sin(angle))
    return scale * np.array(
        [
            value[2, 1] - value[1, 2],
            value[0, 2] - value[2, 0],
            value[1, 0] - value[0, 1],
        ],
        dtype=np.float64,
    )


def _rigid_inverse(transform: np.ndarray) -> np.ndarray:
    value = np.asarray(transform, dtype=np.float64).reshape(4, 4)
    result = np.eye(4, dtype=np.float64)
    result[:3, :3] = value[:3, :3].T
    result[:3, 3] = -(result[:3, :3] @ value[:3, 3])
    return result


def _quaternion_from_rotation(rotation: np.ndarray) -> np.ndarray:
    value = np.asarray(rotation, dtype=np.float64).reshape(3, 3)
    trace = float(np.trace(value))
    if trace > 0.0:
        scale = 2.0 * math.sqrt(trace + 1.0)
        quaternion = np.array(
            [
                (value[2, 1] - value[1, 2]) / scale,
                (value[0, 2] - value[2, 0]) / scale,
                (value[1, 0] - value[0, 1]) / scale,
                0.25 * scale,
            ],
            dtype=np.float64,
        )
    else:
        diagonal = int(np.argmax(np.diag(value)))
        if diagonal == 0:
            scale = 2.0 * math.sqrt(
                max(0.0, 1.0 + value[0, 0] - value[1, 1] - value[2, 2])
            )
            quaternion = np.array(
                [
                    0.25 * scale,
                    (value[0, 1] + value[1, 0]) / scale,
                    (value[0, 2] + value[2, 0]) / scale,
                    (value[2, 1] - value[1, 2]) / scale,
                ],
                dtype=np.float64,
            )
        elif diagonal == 1:
            scale = 2.0 * math.sqrt(
                max(0.0, 1.0 + value[1, 1] - value[0, 0] - value[2, 2])
            )
            quaternion = np.array(
                [
                    (value[0, 1] + value[1, 0]) / scale,
                    0.25 * scale,
                    (value[1, 2] + value[2, 1]) / scale,
                    (value[0, 2] - value[2, 0]) / scale,
                ],
                dtype=np.float64,
            )
        else:
            scale = 2.0 * math.sqrt(
                max(0.0, 1.0 + value[2, 2] - value[0, 0] - value[1, 1])
            )
            quaternion = np.array(
                [
                    (value[0, 2] + value[2, 0]) / scale,
                    (value[1, 2] + value[2, 1]) / scale,
                    0.25 * scale,
                    (value[1, 0] - value[0, 1]) / scale,
                ],
                dtype=np.float64,
            )
    norm = float(np.linalg.norm(quaternion))
    if norm < 1e-12 or not np.isfinite(norm):
        raise ValueError("rotation produced an invalid quaternion")
    return quaternion / norm


def _sample_from_matrix(stamp_ns: int, transform: np.ndarray) -> PoseSample:
    value = np.asarray(transform, dtype=np.float64).reshape(4, 4)
    return PoseSample(
        stamp_ns=int(stamp_ns),
        translation=value[:3, 3].copy(),
        orientation_xyzw=_quaternion_from_rotation(value[:3, :3]),
    )


@dataclass(frozen=True)
class VioContinuityConfig:
    """Conservative gates for recognizing one isolated VIO frame reset."""

    translation_threshold_m: float = 0.03
    rotation_threshold_deg: float = 5.0
    confirmation_frames: int = 4
    max_gap_ms: float = 50.0
    max_confirmation_linear_speed_m_s: float = 3.0
    max_confirmation_angular_speed_deg_s: float = 540.0

    def __post_init__(self) -> None:
        positive = (
            self.translation_threshold_m,
            self.rotation_threshold_deg,
            self.max_gap_ms,
            self.max_confirmation_linear_speed_m_s,
            self.max_confirmation_angular_speed_deg_s,
        )
        if any(not np.isfinite(value) or value <= 0.0 for value in positive):
            raise ValueError("VIO continuity thresholds must be positive and finite")
        if self.confirmation_frames < 2:
            raise ValueError("VIO continuity confirmation_frames must be at least 2")


@dataclass
class _PendingReset:
    raw_samples: list[PoseSample]
    correction: np.ndarray
    innovation_translation_m: float
    innovation_rotation_deg: float


class VioContinuityStitcher:
    """Map successive raw VIO frames into one continuous local coordinate frame.

    A candidate is held briefly and accepted only if the following raw poses form
    a physically plausible new segment. Timestamp gaps deliberately disable
    stitching because the motion across an unobserved interval is ambiguous.
    """

    def __init__(self, config: VioContinuityConfig) -> None:
        self.config = config
        self._correction = np.eye(4, dtype=np.float64)
        self._history: list[PoseSample] = []
        self._last_raw: Optional[PoseSample] = None
        self._pending: Optional[_PendingReset] = None
        self.stitch_events = 0
        self.rejected_candidates = 0
        self.timestamp_resets = 0
        self.tracking_gaps = 0
        self.last_event_stamp_ns: Optional[int] = None
        self.last_event_translation_m = 0.0
        self.last_event_rotation_deg = 0.0

    @property
    def confirming(self) -> bool:
        return self._pending is not None

    @property
    def correction(self) -> np.ndarray:
        return self._correction.copy()

    def reset(self) -> None:
        self._correction = np.eye(4, dtype=np.float64)
        self._history.clear()
        self._last_raw = None
        self._pending = None
        self.stitch_events = 0
        self.rejected_candidates = 0
        self.timestamp_resets = 0
        self.tracking_gaps = 0
        self.last_event_stamp_ns = None
        self.last_event_translation_m = 0.0
        self.last_event_rotation_deg = 0.0

    def status(self) -> dict[str, object]:
        correction_translation = float(np.linalg.norm(self._correction[:3, 3]))
        correction_rotation = rotation_distance_deg(
            np.eye(4, dtype=np.float64), self._correction
        )
        return {
            "state": "confirming" if self.confirming else "tracking",
            "events": self.stitch_events,
            "rejected_candidates": self.rejected_candidates,
            "timestamp_resets": self.timestamp_resets,
            "tracking_gaps": self.tracking_gaps,
            "pending_frames": (
                0 if self._pending is None else len(self._pending.raw_samples)
            ),
            "correction_translation_m": round(correction_translation, 4),
            "correction_rotation_deg": round(correction_rotation, 3),
            "last_event_stamp_ns": self.last_event_stamp_ns,
            "last_event_translation_m": round(self.last_event_translation_m, 4),
            "last_event_rotation_deg": round(self.last_event_rotation_deg, 3),
        }

    def push(self, sample: PoseSample) -> list[PoseSample]:
        """Consume one raw pose and return zero or more ordered corrected poses."""

        raw_transform = matrix_from_pose(sample)
        if self._last_raw is not None and sample.stamp_ns <= self._last_raw.stamp_ns:
            self.timestamp_resets += 1
            self._correction = np.eye(4, dtype=np.float64)
            self._history.clear()
            self._pending = None
            self._last_raw = sample
            corrected = _sample_from_matrix(sample.stamp_ns, raw_transform)
            self._remember((corrected,))
            return [corrected]

        if self._pending is not None:
            return self._continue_pending(sample, raw_transform)

        if self._last_raw is not None:
            gap_ms = (sample.stamp_ns - self._last_raw.stamp_ns) / 1e6
            if gap_ms > self.config.max_gap_ms:
                self.tracking_gaps += 1
                self._history.clear()
                self._last_raw = sample
                corrected = self._correct(sample, raw_transform)
                self._remember((corrected,))
                return [corrected]

        provisional = self._correction @ raw_transform
        expected = (
            provisional
            if not self._history
            else self._expected_transform(sample.stamp_ns)
        )
        innovation_translation = float(
            np.linalg.norm(provisional[:3, 3] - expected[:3, 3])
        )
        innovation_rotation = rotation_distance_deg(expected, provisional)
        self._last_raw = sample
        if len(self._history) >= 2 and (
            innovation_translation >= self.config.translation_threshold_m
            or innovation_rotation >= self.config.rotation_threshold_deg
        ):
            self._pending = _PendingReset(
                raw_samples=[sample],
                correction=expected @ _rigid_inverse(raw_transform),
                innovation_translation_m=innovation_translation,
                innovation_rotation_deg=innovation_rotation,
            )
            return []

        corrected = _sample_from_matrix(sample.stamp_ns, provisional)
        self._remember((corrected,))
        return [corrected]

    def _continue_pending(
        self, sample: PoseSample, raw_transform: np.ndarray
    ) -> list[PoseSample]:
        pending = self._pending
        assert pending is not None
        previous = pending.raw_samples[-1]
        dt_sec = (sample.stamp_ns - previous.stamp_ns) / 1e9
        if dt_sec <= 0.0:
            self.timestamp_resets += 1
            self.rejected_candidates += 1
            self._correction = np.eye(4, dtype=np.float64)
            self._history.clear()
            self._pending = None
            self._last_raw = sample
            corrected = _sample_from_matrix(sample.stamp_ns, raw_transform)
            self._remember((corrected,))
            return [corrected]
        if dt_sec * 1000.0 > self.config.max_gap_ms:
            self.tracking_gaps += 1
            return self._reject_pending(sample, raw_transform, reset_history=True)

        previous_transform = matrix_from_pose(previous)
        relative = _rigid_inverse(previous_transform) @ raw_transform
        linear_speed = float(np.linalg.norm(relative[:3, 3])) / dt_sec
        angular_speed = rotation_distance_deg(
            np.eye(4, dtype=np.float64), relative
        ) / dt_sec
        if (
            linear_speed > self.config.max_confirmation_linear_speed_m_s
            or angular_speed > self.config.max_confirmation_angular_speed_deg_s
        ):
            return self._reject_pending(sample, raw_transform, reset_history=False)

        pending.raw_samples.append(sample)
        self._last_raw = sample
        if len(pending.raw_samples) < self.config.confirmation_frames:
            return []

        self._correction = pending.correction
        corrected = tuple(
            self._correct(raw, matrix_from_pose(raw))
            for raw in pending.raw_samples
        )
        self.stitch_events += 1
        self.last_event_stamp_ns = pending.raw_samples[0].stamp_ns
        self.last_event_translation_m = pending.innovation_translation_m
        self.last_event_rotation_deg = pending.innovation_rotation_deg
        self._pending = None
        self._remember(corrected)
        return list(corrected)

    def _reject_pending(
        self,
        sample: PoseSample,
        raw_transform: np.ndarray,
        *,
        reset_history: bool,
    ) -> list[PoseSample]:
        pending = self._pending
        assert pending is not None
        raw_samples = [*pending.raw_samples, sample]
        self._pending = None
        self._last_raw = sample
        self.rejected_candidates += 1
        if reset_history:
            self._history.clear()
        corrected = tuple(
            self._correct(
                raw,
                raw_transform if raw is sample else matrix_from_pose(raw),
            )
            for raw in raw_samples
        )
        self._remember(corrected)
        return list(corrected)

    def _correct(
        self, sample: PoseSample, raw_transform: Optional[np.ndarray] = None
    ) -> PoseSample:
        transform = matrix_from_pose(sample) if raw_transform is None else raw_transform
        return _sample_from_matrix(sample.stamp_ns, self._correction @ transform)

    def _expected_transform(self, stamp_ns: int) -> np.ndarray:
        latest = matrix_from_pose(self._history[-1])
        if len(self._history) < 2:
            return latest
        previous = matrix_from_pose(self._history[-2])
        history_dt = self._history[-1].stamp_ns - self._history[-2].stamp_ns
        prediction_dt = int(stamp_ns) - self._history[-1].stamp_ns
        if history_dt <= 0 or prediction_dt <= 0:
            return latest
        scale = min(float(prediction_dt / history_dt), 3.0)
        predicted = latest.copy()
        velocity = (
            self._history[-1].translation - self._history[-2].translation
        ) / (history_dt / 1e9)
        predicted[:3, 3] += velocity * (prediction_dt / 1e9)
        relative_rotation = previous[:3, :3].T @ latest[:3, :3]
        predicted[:3, :3] = latest[:3, :3] @ _so3_exp(
            scale * _so3_log(relative_rotation)
        )
        return predicted

    def _remember(self, samples: tuple[PoseSample, ...]) -> None:
        self._history.extend(samples)
        if len(self._history) > 3:
            del self._history[:-3]
