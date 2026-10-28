"""Camera-anchored, full-covariance fusion of visual map-to-odom corrections."""

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


def _pose_error_jacobian(correction: np.ndarray, odom_to_camera: np.ndarray) -> np.ndarray:
    """Map correction errors to world position and local camera rotation errors."""
    jacobian = np.eye(6, dtype=np.float64)
    jacobian[:3, 3:] = -correction[:3, :3] @ _skew(odom_to_camera[:3, 3])
    jacobian[3:, 3:] = odom_to_camera[:3, :3].T
    return jacobian


def correction_covariance(
    correction: np.ndarray, odom_to_camera: np.ndarray,
    translation_std_m: float, rotation_std_deg: float,
) -> np.ndarray:
    """Express independent camera-pose noise in map-to-odom correction coordinates."""
    inverse = np.linalg.inv(_pose_error_jacobian(correction, odom_to_camera))
    noise = np.diag([float(translation_std_m)**2] * 3
        + [math.radians(float(rotation_std_deg))**2] * 3)
    return inverse @ noise @ inverse.T


@dataclass(frozen=True)
class RelocalizationEkfConfig:
    """Physical camera-pose process noise per square-root second."""
    process_translation_std_m_sqrt_s: float = 0.02
    process_rotation_std_deg_sqrt_s: float = 0.5

    def __post_init__(self) -> None:
        values = (self.process_translation_std_m_sqrt_s, self.process_rotation_std_deg_sqrt_s)
        if any(not np.isfinite(value) or value <= 0.0 for value in values):
            raise ValueError("EKF process noise must be positive")


class RelocalizationEkf:
    """Fuse visual correction observations with full position/rotation covariance.

    The state is T_map_odom. Updates are linearized at the observed camera pose,
    preserving rotation/translation correlations independently of the odom origin.
    """
    def __init__(self, config: RelocalizationEkfConfig) -> None:
        self.config = config
        self._estimate: Optional[np.ndarray] = None
        self._covariance = np.zeros((6, 6), dtype=np.float64)
        self.last_innovation_translation_m = 0.0
        self.last_innovation_rotation_deg = 0.0

    @property
    def initialized(self) -> bool:
        return self._estimate is not None

    @property
    def correction(self) -> Optional[np.ndarray]:
        return None if self._estimate is None else self._estimate.copy()

    @property
    def covariance_diagonal(self) -> list[float]:
        return np.diag(self._covariance).astype(float).tolist()

    def reset(self) -> None:
        self._estimate = None
        self._covariance.fill(0.0)
        self.last_innovation_translation_m = 0.0
        self.last_innovation_rotation_deg = 0.0

    def reinitialize(self, map_to_odom: np.ndarray) -> None:
        self._estimate = _valid_transform(map_to_odom)
        self._covariance.fill(0.0)
        self.last_innovation_translation_m = 0.0
        self.last_innovation_rotation_deg = 0.0

    def predict(self, dt_sec: float, odom_to_camera: np.ndarray) -> None:
        if not self.initialized or not np.isfinite(dt_sec) or dt_sec <= 0.0:
            return
        scale = math.sqrt(min(float(dt_sec), 1.0))
        self._covariance += correction_covariance(
            self._estimate, odom_to_camera,
            self.config.process_translation_std_m_sqrt_s * scale,
            self.config.process_rotation_std_deg_sqrt_s * scale)

    def observe(
        self, map_to_odom: np.ndarray, odom_to_camera: np.ndarray,
        measurement_covariance: np.ndarray,
    ) -> bool:
        """Update from one validated visual measurement and its full 6x6 covariance."""
        measurement = _valid_transform(map_to_odom)
        odom = _valid_transform(odom_to_camera)
        covariance = np.asarray(measurement_covariance, dtype=np.float64).reshape(6, 6)
        covariance = .5 * (covariance + covariance.T)
        if not np.isfinite(covariance).all():
            raise ValueError("measurement covariance contains non-finite values")
        np.linalg.cholesky(covariance)
        if not self.initialized:
            self.reinitialize(measurement)
            return True

        predicted_pose = self._estimate @ odom
        measured_pose = measurement @ odom
        prior_jacobian = _pose_error_jacobian(self._estimate, odom)
        measurement_jacobian = _pose_error_jacobian(measurement, odom)
        prior = prior_jacobian @ self._covariance @ prior_jacobian.T
        observed = measurement_jacobian @ covariance @ measurement_jacobian.T
        innovation = np.r_[measured_pose[:3, 3] - predicted_pose[:3, 3],
            _so3_log(predicted_pose[:3, :3].T @ measured_pose[:3, :3])]
        rotation_cross = _skew(innovation[3:])
        tangent = np.eye(6)
        tangent[3:, 3:] += .5 * rotation_cross + rotation_cross @ rotation_cross / 12.
        observed = tangent @ observed @ tangent.T
        gain = np.linalg.solve((prior + observed).T, prior.T).T
        delta = gain @ innovation
        predicted_pose[:3, 3] += delta[:3]
        predicted_pose[:3, :3] = predicted_pose[:3, :3] @ _so3_exp(delta[3:])
        self._estimate = predicted_pose @ np.linalg.inv(odom)
        identity_minus_gain = np.eye(6) - gain
        posterior = identity_minus_gain @ prior @ identity_minus_gain.T + gain @ observed @ gain.T
        reset = np.eye(6)
        reset[3:, 3:] -= .5 * _skew(delta[3:])
        inverse = np.linalg.inv(_pose_error_jacobian(self._estimate, odom))
        transport = inverse @ reset
        self._covariance = transport @ posterior @ transport.T
        self._covariance = .5 * (self._covariance + self._covariance.T)
        self.last_innovation_translation_m = float(np.linalg.norm(innovation[:3]))
        self.last_innovation_rotation_deg = math.degrees(float(np.linalg.norm(innovation[3:])))
        return False
