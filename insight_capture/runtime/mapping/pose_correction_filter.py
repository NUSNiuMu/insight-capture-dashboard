"""Camera-centred error-state EKF and continuous injection of small corrections."""
import numpy as np
from scipy.spatial.transform import Rotation


def _skew(v):
    x, y, z = v
    return np.array([[0., -z, y], [z, 0., -x], [-y, x, 0.]])


def _innovation(estimate, measurement):
    return np.r_[measurement[:3, 3] - estimate[:3, 3],
                 Rotation.from_matrix(measurement[:3, :3] @ estimate[:3, :3].T).as_rotvec()]


class PoseCorrectionFilter:
    def __init__(self):
        self.posterior = self.emitted = self.raw = None
        self.covariance = np.diag([.003**2] * 3 + [np.deg2rad(.3)**2] * 3)
        self.measurement_noise = self.covariance.copy()
        self.native = None
        self.anchor = None
        self.source_ns = None
        self.influence = np.zeros((6, 6))
        self.cov_stamp = self.output_stamp = 0
        self.valid = False
        self.diagnostics = {"mode": "uninitialized"}

    def suspend(self, reason):
        self.valid = False
        self.diagnostics = {"mode": "invalid", "reason": reason}

    def reject(self, reason):
        self.diagnostics = {"mode": "predicting" if self.valid else "uninitialized",
                            "accepted": False, "reason": reason}

    def _predict(self, native, stamp):
        if self.posterior is None or stamp <= self.cov_stamp:
            return
        dt = (stamp - self.cov_stamp) / 1e9
        motion = (self.posterior @ native)[:3, 3] - (self.posterior @ self.native)[:3, 3]
        transition = np.eye(6)
        transition[:3, 3:] = -_skew(motion)
        noise = np.diag([.01**2] * 3 + [np.deg2rad(.5)**2] * 3) * dt
        self.covariance = transition @ self.covariance @ transition.T + noise
        self.influence = transition @ self.influence
        self.native, self.cov_stamp = native.copy(), stamp

    def observe(self, correction, native, stamp, source_ns, *, verified=False):
        if self.posterior is None:
            self.posterior = self.emitted = correction.copy()
            self.native, self.cov_stamp, self.output_stamp = native.copy(), stamp, stamp
            self.valid = True
            self.influence = np.eye(6)
            mode, accepted, distance = "initialized", True, 0.
        else:
            self._predict(native, stamp)
            estimate, measurement = self.posterior @ native, correction @ native
            innovation = _innovation(estimate, measurement)
            distance = float(np.linalg.norm(innovation[:3]))
            if distance <= .05:
                gain = np.linalg.solve((self.covariance + self.measurement_noise).T,
                                       self.covariance.T).T
                delta = gain @ innovation
                estimate[:3, 3] += delta[:3]
                estimate[:3, :3] = Rotation.from_rotvec(delta[3:]).as_matrix() @ estimate[:3, :3]
                self.posterior = estimate @ np.linalg.inv(native)
                residual = np.eye(6) - gain
                covariance = residual @ self.covariance @ residual.T + gain @ self.measurement_noise @ gain.T
                reset = np.eye(6)
                reset[3:, 3:] += .5 * _skew(delta[3:])
                self.covariance = reset @ covariance @ reset.T
                self.influence = reset @ gain
                self.valid = True
                mode, accepted = "ekf", True
            else:
                if verified:
                    self.posterior = correction.copy()
                    self.covariance = self.measurement_noise.copy()
                    self.influence = np.eye(6)
                    self.valid = True
                    mode, accepted = "relocalizing", True
                else:
                    mode, accepted = "awaiting_relocalization", False
        if accepted:
            self.raw = correction.copy()
            self.anchor = native.copy()
            self.source_ns = source_ns
        self.diagnostics = {"mode": mode, "accepted": accepted, "innovation_mm": distance * 1000}
        return accepted

    def revise(self, correction, native, stamp, source_ns):
        # Replace this image's contribution, without counting its revised pose twice.
        if self.raw is None or source_ns != self.source_ns:
            self.reject("unmatched_map_observation")
            return False
        self._predict(native, stamp)
        distance = float(np.linalg.norm(_innovation(self.raw @ native, correction @ native)[:3]))
        if distance <= .05:
            delta = self.influence @ _innovation(self.raw @ self.anchor, correction @ self.anchor)
            estimate = self.posterior @ native
            estimate[:3, 3] += delta[:3]
            estimate[:3, :3] = Rotation.from_rotvec(delta[3:]).as_matrix() @ estimate[:3, :3]
            self.posterior = estimate @ np.linalg.inv(native)
            reset = np.eye(6)
            reset[3:, 3:] += .5 * _skew(delta[3:])
            self.covariance = reset @ self.covariance @ reset.T
            self.influence = reset @ self.influence
            mode = "map_revision"
        else:
            mode = "awaiting_relocalization"
        if distance <= .05:
            self.raw = correction.copy()
        self.diagnostics = {"mode": mode, "accepted": distance <= .05,
                            "innovation_mm": distance * 1000}
        return distance <= .05

    def output(self, native, stamp, *, speed=.15, lever_arm=0.):
        if not self.valid:
            return None
        self._predict(native, stamp)
        dt = min(1. / 30., max(0., (stamp - self.output_stamp) / 1e9))
        target, emitted = self.posterior @ native, self.emitted @ native
        difference = _innovation(emitted, target)
        distance, angle = np.linalg.norm(difference[:3]), np.linalg.norm(difference[3:])
        # Inject the posterior in camera coordinates; native VIO motion is never smoothed.
        fraction = min(1. - np.exp(-dt / .05), speed * dt / max(distance + angle * lever_arm, 1e-12),
                       np.deg2rad(15.) * dt / max(angle, 1e-12))
        emitted[:3, 3] += fraction * difference[:3]
        emitted[:3, :3] = Rotation.from_rotvec(fraction * difference[3:]).as_matrix() @ emitted[:3, :3]
        self.emitted = emitted @ np.linalg.inv(native)
        self.output_stamp = max(self.output_stamp, stamp)
        self.diagnostics["injection_remaining_mm"] = float(np.linalg.norm(target[:3, 3] - emitted[:3, 3]) * 1000)
        return self.emitted.copy()
