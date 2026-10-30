"""Keep SDK translation revisions out of the continuous motion coordinate frame."""

from collections import deque

import numpy as np

from .geometry import PoseSample


class FrameOdometry:
    def __init__(self):
        self.imu = deque(maxlen=1000)
        self.last = self.velocity = None
        self.interval = self.acceleration = 0.
        self.offset = np.zeros(3)
        self.revisions = 0
        self.diagnostics = {"revision_count": 0}

    def add_imu(self, stamp, acceleration):
        self.imu.append((stamp, float(np.linalg.norm(acceleration))))

    def update(self, sample):
        if self.last is not None and sample.stamp_ns <= self.last.stamp_ns:
            return None
        position = sample.translation + self.offset
        revised = False
        if self.last is not None:
            dt = (sample.stamp_ns - self.last.stamp_ns) / 1e9
            while len(self.imu) > 1 and self.imu[1][0] < self.last.stamp_ns:
                self.imu.popleft()
            acceleration = max((a for _, a in self.imu), default=0.) + 9.81
            if self.velocity is not None:
                predicted = self.last.translation + self.velocity * dt
                # Reuse the 5 cm correction boundary, enlarged by the measured IMU acceleration.
                motion_bound = .5 * max(acceleration, self.acceleration) * dt * (dt + self.interval)
                revised = np.linalg.norm(position - predicted) > .05 + motion_bound
                if revised:
                    self.offset += predicted - position
                    position = predicted
                    self.revisions += 1
            self.velocity = (position - self.last.translation) / dt
            self.interval, self.acceleration = dt, acceleration
        self.last = PoseSample(sample.stamp_ns, position.copy(), sample.orientation_xyzw.copy())
        self.diagnostics = {"revision_count": self.revisions, "translation_revision": bool(revised)}
        return self.last
