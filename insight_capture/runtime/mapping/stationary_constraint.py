"""Fuse independent visual and VIO rest evidence without changing raw VIO."""

from collections import Counter, deque
from dataclasses import dataclass

import cv2
import numpy as np

from .geometry import rotation_distance_deg


class VisualRestEvidence:
    """Compare against a fixed image anchor, including accumulated slow motion."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.image = self.points = None
        self.quiet_since = None
        self.last_time = None

    def observe(self, image, now, mask_height_ratio=0.2):
        if self.last_time is not None and not 0 < now - self.last_time <= 1.5:
            self.reset()
        self.last_time = now
        result = dict(quiet=False, duration_sec=0.0, tracks=0, grid_cells=0)
        if self.image is not None and self.image.shape == image.shape and self.points is not None:
            forward, valid, _ = cv2.calcOpticalFlowPyrLK(
                self.image, image, self.points, None, winSize=(21, 21), maxLevel=2)
            backward, reverse_valid, _ = cv2.calcOpticalFlowPyrLK(
                image, self.image, forward, None, winSize=(21, 21), maxLevel=2)
            keep = ((valid[:, 0] != 0) & (reverse_valid[:, 0] != 0)
                    & np.isfinite(forward).all(axis=(1, 2))
                    & (np.linalg.norm((backward - self.points).reshape(-1, 2), axis=1) <= .5))
            displacement = np.linalg.norm((forward - self.points).reshape(-1, 2), axis=1)[keep]
            pixels = self.points.reshape(-1, 2)[keep]
            cells = len(set(map(tuple, (pixels * [4 / image.shape[1], 4 / image.shape[0]]).astype(int))))
            result.update(tracks=len(pixels), grid_cells=cells)
            if len(displacement):
                median, high = np.quantile(displacement, [.5, .9])
                result.update(median_flow_px=float(median), p90_flow_px=float(high))
                result["quiet"] = bool(len(pixels) >= 30 and len(pixels) >= .6 * len(self.points)
                                       and cells >= 6 and median <= .5 and high <= 1.0)
            if result["quiet"]:
                result["duration_sec"] = now - self.quiet_since
                return result
        mask = np.zeros_like(image)
        mask[:int(image.shape[0] * (1 - mask_height_ratio))] = 255
        self.points = cv2.goodFeaturesToTrack(image, 120, .02, 12, mask=mask)
        self.image = image.copy()
        self.quiet_since = now
        return result


@dataclass(frozen=True)
class StationaryConfig:
    confirmation_sec: float = 1.2
    image_expiry_sec: float = 2.0
    pose_gap_sec: float = .15
    pose_window_sec: float = .35
    max_velocity_m_s: float = .003
    max_pose_step_m: float = .002
    max_pose_step_deg: float = .2


class StationaryPoseConstraint:
    """Hold a visually verified world pose and compensate local VIO drift.

    The caller serializes access with its camera lock. Only a previously
    localized camera can be constrained; movement releases on the VIO path.
    """

    def __init__(self, config=StationaryConfig()):
        self.config = config
        self.entries = self.releases = self.epoch = 0
        self.release_reasons = Counter()
        self.reset()

    def reset(self):
        self.epoch += 1
        self.anchor = None
        self.evidence = {}
        self.image_time = float("-inf")
        self.last_pose_time = None
        self.window = deque()
        self.reason = "waiting_evidence"
        self.velocity_m_s = 0.0

    @property
    def active(self):
        return self.anchor is not None

    def observe_image(self, evidence, now):
        self.evidence = dict(evidence)
        self.image_time = now
        if not evidence.get("quiet"):
            self.release("visual_motion_or_insufficient_features")

    def release(self, reason):
        if self.active:
            self.releases += 1
            self.release_reasons[reason] += 1
        self.anchor = None
        self.reason = reason

    def observe_pose(self, pose, correction, now, device_static, stamp_sec=None, entry_allowed=True):
        pose_time = now if stamp_sec is None else stamp_sec
        previous_active = self.active
        if self.last_pose_time is not None and not 0 < pose_time - self.last_pose_time <= self.config.pose_gap_sec:
            self.window.clear()
            self.evidence = {}
            self.release("vio_gap")
        self.last_pose_time = pose_time
        while self.window and pose_time - self.window[0][0] > self.config.pose_window_sec:
            self.window.popleft()
        moving = False
        if len(self.window) >= 3:
            positions = np.array([value[:3, 3] for _, value in self.window])
            moving = (np.linalg.norm(pose[:3, 3] - np.median(positions, axis=0)) > self.config.max_pose_step_m
                      or rotation_distance_deg(self.window[len(self.window) // 2][1], pose)
                      > self.config.max_pose_step_deg)
            if len(self.window) >= 8:
                times = np.array([stamp for stamp, _ in self.window])
                times -= times.mean()
                velocity = times @ positions / max(float(times @ times), 1e-12)
                self.velocity_m_s = float(np.linalg.norm(velocity))
                moving |= self.velocity_m_s > self.config.max_velocity_m_s
        self.window.append((pose_time, pose.copy()))
        if not device_static:
            self.evidence = {}
            self.release("device_not_static")
        elif moving:
            self.evidence = {}
            self.release("vio_motion")
        elif now - self.image_time > self.config.image_expiry_sec:
            self.release("image_expired")
        elif correction is not None and len(self.window) >= 4 and self.evidence.get("quiet") and (
            self.evidence.get("duration_sec", 0) >= self.config.confirmation_sec
        ):
            if not self.active and entry_allowed:
                self.anchor = correction @ pose
                self.entries += 1
            self.reason = "verified_stationary" if self.active else "waiting_map_confirmation"
        value = self.anchor @ np.linalg.inv(pose) if self.active else None
        return value, previous_active != self.active

    def status(self):
        return dict(active=self.active, reason=self.reason, entries=self.entries,
                    releases=self.releases, release_reasons=dict(self.release_reasons),
                    velocity_m_s=self.velocity_m_s, visual=dict(self.evidence))
