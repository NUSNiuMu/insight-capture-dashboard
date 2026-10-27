"""Confirm bounded head corrections against historical map geometry."""

from dataclasses import replace
import time

import numpy as np

from .geometry import rotation_distance_deg
from .global_localization import LocalizationConsensus, localize_features


class HeadMapTracker:
    """Keep head observations separate from keyframe and loop-closure cadence."""

    def __init__(self, config, *, max_translation_m=.15, max_rotation_deg=5.0):
        if not (np.isfinite(max_translation_m) and max_translation_m > 0
                and np.isfinite(max_rotation_deg) and max_rotation_deg > 0):
            raise ValueError("Head innovation bounds must be finite and positive")
        self.config = replace(config, confirmation_translation_m=.05,
                              confirmation_rotation_deg=3.0)
        self.max_translation_m = max_translation_m
        self.max_rotation_deg = max_rotation_deg
        self.reset()

    def reset(self):
        self.consensus = LocalizationConsensus(self.config)
        self.last_measurement = None
        self.last_stamp_ns = -1
        self.last_confirmed_ns = -1
        self.last_confirmed_monotonic = None
        self.accepted = 0
        self.status = dict(accepted=False, rejection="waiting_for_historical_map")

    def observe(self, stamp_ns, keypoints, descriptors, map_points, map_descriptors,
                camera_matrix, odom_to_camera, image_shape, current_correction):
        if stamp_ns <= self.last_stamp_ns:
            return None, dict(accepted=False, rejection="stale_head_image")
        self.last_stamp_ns = stamp_ns
        candidate, diagnostics = localize_features(
            keypoints, descriptors, map_points, map_descriptors, camera_matrix,
            odom_to_camera, image_shape, self.config, map_descriptors_normalized=True)
        diagnostics["geometric_accepted"] = candidate is not None
        if candidate is not None:
            translation = float(np.linalg.norm(candidate.map_to_odom[:3, 3] - current_correction[:3, 3]))
            rotation = rotation_distance_deg(candidate.map_to_odom, current_correction)
            diagnostics.update(innovation_translation_m=round(translation, 4),
                               innovation_rotation_deg=round(rotation, 3))
            if translation > self.max_translation_m or rotation > self.max_rotation_deg:
                candidate = None
                diagnostics.update(accepted=False, rejection="head_innovation_out_of_bounds")
        transition = self.consensus.observe(candidate)
        measurement = self.consensus.correction
        confirmed = bool(candidate is not None and measurement is not None
                         and not transition["pose_ambiguous"]
                         and self.consensus.consistent(candidate.map_to_odom, measurement))
        result = None
        if confirmed:
            self.last_confirmed_ns = stamp_ns
            self.last_confirmed_monotonic = time.monotonic()
            if self.last_measurement is None or not np.array_equal(measurement, self.last_measurement):
                self.last_measurement = measurement.copy()
                result = measurement.copy()
                self.accepted += 1
        elif diagnostics.get("accepted"):
            diagnostics["rejection"] = "head_pose_consensus_pending"
        diagnostics.update(accepted=confirmed, confirmation_progress=transition["confirmation_progress"],
                           confirmation_required=transition["confirmation_required"],
                           updates=self.accepted, image_stamp_ns=int(stamp_ns))
        self.status = diagnostics
        return result, diagnostics

    def snapshot(self, stamp_ns, *, freshness_sec=1.5):
        age = None if self.last_confirmed_ns < 0 else (stamp_ns - self.last_confirmed_ns) / 1e9
        wall_age = (None if self.last_confirmed_monotonic is None
                    else max(0., time.monotonic() - self.last_confirmed_monotonic))
        fresh = age is not None and 0 <= age <= freshness_sec and wall_age <= freshness_sec
        return dict(self.status, observation_age_sec=age, observation_wall_age_sec=wall_age,
                    tracking_mode="map_matched" if fresh else "vio_only")
