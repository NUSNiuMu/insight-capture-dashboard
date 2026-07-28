"""Error-state EKF for a continuous map-to-VIO-odometry correction."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np


def _skew(vector: np.ndarray) -> np.ndarray:
    x, y, z = np.asarray(vector, dtype=np.float64).reshape(3)
    return np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]])


def _so3_exp(rotation_vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(rotation_vector, dtype=np.float64).reshape(3)
    angle = float(np.linalg.norm(vector))
    cross = _skew(vector)
    if angle < 1e-8:
        return np.eye(3) + cross + 0.5 * cross @ cross
    sine_scale = math.sin(angle) / angle
    cosine_scale = (1.0 - math.cos(angle)) / (angle * angle)
    return np.eye(3) + sine_scale * cross + cosine_scale * cross @ cross


def _so3_log(rotation: np.ndarray) -> np.ndarray:
    value = np.asarray(rotation, dtype=np.float64).reshape(3, 3)
    cosine = float(np.clip((np.trace(value) - 1.0) * 0.5, -1.0, 1.0))
    angle = math.acos(cosine)
    vee = np.array(
        [
            value[2, 1] - value[1, 2],
            value[0, 2] - value[2, 0],
            value[1, 0] - value[0, 1],
        ],
        dtype=np.float64,
    )
    if angle < 1e-8:
        return 0.5 * vee
    if math.pi - angle < 1e-5:
        eigenvalues, eigenvectors = np.linalg.eigh((value + np.eye(3)) * 0.5)
        axis = eigenvectors[:, int(np.argmax(eigenvalues))]
        if float(axis @ vee) < 0.0:
            axis = -axis
        return angle * axis
    return (angle / (2.0 * math.sin(angle))) * vee


def _valid_transform(transform: np.ndarray) -> np.ndarray:
    value = np.asarray(transform, dtype=np.float64).reshape(4, 4).copy()
    if not np.all(np.isfinite(value)):
        raise ValueError("correction transform contains non-finite values")
    u, _, vt = np.linalg.svd(value[:3, :3])
    value[:3, :3] = u @ vt
    if np.linalg.det(value[:3, :3]) < 0.0:
        u[:, -1] *= -1.0
        value[:3, :3] = u @ vt
    value[3] = (0.0, 0.0, 0.0, 1.0)
    return value


@dataclass(frozen=True)
class RelocalizationEkfConfig:
    process_translation_std_m_sqrt_s: float = 0.02
    process_rotation_std_deg_sqrt_s: float = 0.5
    measurement_translation_std_m: float = 0.10
    measurement_rotation_std_deg: float = 3.0
    correction_time_constant_sec: float = 1.0

    def __post_init__(self) -> None:
        values = (
            self.process_translation_std_m_sqrt_s,
            self.process_rotation_std_deg_sqrt_s,
            self.measurement_translation_std_m,
            self.measurement_rotation_std_deg,
            self.correction_time_constant_sec,
        )
        if any(not np.isfinite(value) or value <= 0.0 for value in values):
            raise ValueError("EKF noise and time-constant values must be positive")


class RelocalizationEkf:
    """Fuse absolute relocalization with continuous VIO motion.

    The state is ``T_map_odom``. VIO supplies the high-rate motion through
    ``T_map_odom @ T_odom_camera`` and therefore only adds process uncertainty
    here. Relocalization supplies an absolute observation of ``T_map_odom``.
    The first observation initializes both states exactly; later EKF updates
    are injected into the published correction with a time constant so a
    low-rate observation cannot create a visible one-frame pose jump.
    """

    def __init__(self, config: RelocalizationEkfConfig) -> None:
        self.config = config
        self._estimate: Optional[np.ndarray] = None
        self._output: Optional[np.ndarray] = None
        self._covariance = np.zeros((6, 6), dtype=np.float64)
        self.last_innovation_translation_m = 0.0
        self.last_innovation_rotation_deg = 0.0

    @property
    def initialized(self) -> bool:
        return self._estimate is not None

    @property
    def correction(self) -> Optional[np.ndarray]:
        return None if self._output is None else self._output.copy()

    @property
    def estimate(self) -> Optional[np.ndarray]:
        """Return the latest filtered target before display smoothing."""

        return None if self._estimate is None else self._estimate.copy()

    @property
    def covariance_diagonal(self) -> list[float]:
        return np.diag(self._covariance).astype(float).tolist()

    def reset(self) -> None:
        self._estimate = None
        self._output = None
        self._covariance.fill(0.0)
        self.last_innovation_translation_m = 0.0
        self.last_innovation_rotation_deg = 0.0

    def reinitialize(self, map_to_odom: np.ndarray) -> None:
        """Immediately replace the estimate and published correction."""

        measurement = _valid_transform(map_to_odom)
        self._estimate = measurement
        self._output = measurement.copy()
        self._covariance.fill(0.0)
        self.last_innovation_translation_m = 0.0
        self.last_innovation_rotation_deg = 0.0

    def predict(self, dt_sec: float) -> None:
        if not self.initialized or not np.isfinite(dt_sec) or dt_sec <= 0.0:
            return
        process_dt = min(float(dt_sec), 1.0)
        translation_variance = (
            self.config.process_translation_std_m_sqrt_s**2 * process_dt
        )
        rotation_std = math.radians(
            self.config.process_rotation_std_deg_sqrt_s
        )
        rotation_variance = rotation_std**2 * process_dt
        self._covariance += np.diag(
            [translation_variance] * 3 + [rotation_variance] * 3
        )

        # Bound the elapsed time used for correction injection. A resumed VIO
        # stream must not consume a long pause as one large correction step.
        smoothing_dt = min(float(dt_sec), 0.1)
        alpha = 1.0 - math.exp(
            -smoothing_dt / self.config.correction_time_constant_sec
        )
        self._output[:3, 3] += alpha * (
            self._estimate[:3, 3] - self._output[:3, 3]
        )
        rotation_error = _so3_log(
            self._output[:3, :3].T @ self._estimate[:3, :3]
        )
        self._output[:3, :3] = (
            self._output[:3, :3] @ _so3_exp(alpha * rotation_error)
        )

    def observe(self, map_to_odom: np.ndarray) -> bool:
        """Apply a relocalization observation.

        Returns ``True`` only for the first observation, which intentionally
        initializes (and therefore jumps) to the real global pose.
        """

        measurement = _valid_transform(map_to_odom)
        if not self.initialized:
            self.reinitialize(measurement)
            return True

        innovation = np.empty(6, dtype=np.float64)
        innovation[:3] = measurement[:3, 3] - self._estimate[:3, 3]
        innovation[3:] = _so3_log(
            self._estimate[:3, :3].T @ measurement[:3, :3]
        )
        self.last_innovation_translation_m = float(
            np.linalg.norm(innovation[:3])
        )
        self.last_innovation_rotation_deg = math.degrees(
            float(np.linalg.norm(innovation[3:]))
        )

        translation_variance = self.config.measurement_translation_std_m**2
        rotation_variance = math.radians(
            self.config.measurement_rotation_std_deg
        ) ** 2
        measurement_covariance = np.diag(
            [translation_variance] * 3 + [rotation_variance] * 3
        )
        innovation_covariance = self._covariance + measurement_covariance
        gain = np.linalg.solve(
            innovation_covariance.T, self._covariance.T
        ).T
        delta = gain @ innovation
        self._estimate[:3, 3] += delta[:3]
        self._estimate[:3, :3] = (
            self._estimate[:3, :3] @ _so3_exp(delta[3:])
        )
        identity_minus_gain = np.eye(6) - gain
        self._covariance = (
            identity_minus_gain
            @ self._covariance
            @ identity_minus_gain.T
            + gain @ measurement_covariance @ gain.T
        )
        return False
