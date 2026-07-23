"""Dashboard coordinate conversion and pose transformation."""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

import numpy as np

from session_alignment import (
    interpolate_pose_sample,
    invert_transform,
    matrix_to_transform,
    transform_point,
    transform_pose_sample,
)


class AlignmentTransforms:
    def __init__(self, owner) -> None:
        self.owner = owner

    def _transform_pose_point(
        self, pose_name: str, point: Tuple[float, float, float]
    ) -> Tuple[float, float, float]:
        if not self.owner.session_alignment_enabled:
            return point
        lock = getattr(self.owner, "live_alignment_solution_lock", None)
        if lock is None:
            transform = self.owner.world_to_reference.get(pose_name)
        else:
            with lock:
                transform = self.owner.world_to_reference.get(pose_name)
        if transform is None:
            return point
        return transform_point(transform, point)

    def transformed_trace(self, pose_name: str) -> List[Tuple[float, float, float]]:
        raw_trace = list(self.owner.raw_traces[pose_name])
        if not self.owner.session_alignment_enabled or not raw_trace:
            return raw_trace
        lock = getattr(self.owner, "live_alignment_solution_lock", None)
        if lock is None:
            transform = self.owner.world_to_reference.get(pose_name)
        else:
            with lock:
                transform = self.owner.world_to_reference.get(pose_name)
        if transform is None:
            return raw_trace
        # One vectorized (N,3) pass instead of a per-point 4x4 matmul + fresh
        # 4-vector allocation: this runs per websocket broadcast tick (20Hz x
        # 3 poses x up to 300 points), which measured ~18k matmuls/sec.
        points = np.asarray(raw_trace, dtype=np.float64)
        mapped = points @ transform[:3, :3].T + transform[:3, 3]
        return mapped.tolist()

    def transformed_pose_sample(self, pose_name: str):
        latest_pose_sample = getattr(self.owner, "latest_pose_sample", {}).get(pose_name)
        if latest_pose_sample is None:
            return None
        if not self.owner.session_alignment_enabled:
            return latest_pose_sample
        lock = getattr(self.owner, "live_alignment_solution_lock", None)
        if lock is None:
            transform = self.owner.world_to_reference.get(pose_name)
        else:
            with lock:
                transform = self.owner.world_to_reference.get(pose_name)
        if transform is None:
            return latest_pose_sample
        return transform_pose_sample(transform, latest_pose_sample)

    @staticmethod
    def _optical_to_dashboard_rotation() -> np.ndarray:
        return np.array(
            [
                [0.0, 0.0, 1.0],
                [1.0, 0.0, 0.0],
                [0.0, -1.0, 0.0],
            ],
            dtype=np.float64,
        )

    def _dashboard_transform_from_optical(self, optical_transform: np.ndarray) -> np.ndarray:
        rotation_map = self.owner._optical_to_dashboard_rotation()
        rotation = rotation_map @ optical_transform[:3, :3] @ rotation_map.T
        translation = rotation_map @ optical_transform[:3, 3]
        return matrix_to_transform(rotation, translation)

    @staticmethod
    def _rotation_about_display_z(yaw_deg: float) -> np.ndarray:
        yaw_rad = math.radians(float(yaw_deg))
        cos_yaw = math.cos(yaw_rad)
        sin_yaw = math.sin(yaw_rad)
        return np.array(
            [
                [cos_yaw, -sin_yaw, 0.0],
                [sin_yaw, cos_yaw, 0.0],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )

    def _dashboard_horizontal_yaw_deg_from_transforms(self, transforms: Dict[str, np.ndarray]) -> float:
        if self.owner.live_alignment_dashboard_horizontal_yaw_mode == "manual":
            return self.owner.live_alignment_dashboard_horizontal_yaw_deg
        if self.owner.live_alignment_dashboard_horizontal_yaw_mode != "auto_center_non_reference":
            return 0.0
        horizontal_vectors = []
        for camera in self.owner.cameras:
            if camera.name == self.owner.reference_camera:
                continue
            transform = transforms.get(camera.name)
            if transform is None:
                continue
            translation = transform[:3, 3]
            horizontal_vectors.append((float(translation[0]), float(translation[1])))
        if not horizontal_vectors:
            return 0.0
        mean_forward = sum(item[0] for item in horizontal_vectors) / len(horizontal_vectors)
        mean_right = sum(item[1] for item in horizontal_vectors) / len(horizontal_vectors)
        if abs(mean_forward) < 1e-9 and abs(mean_right) < 1e-9:
            return 0.0
        return -math.degrees(math.atan2(mean_right, mean_forward))

    def _apply_dashboard_horizontal_yaw(
        self,
        transforms: Dict[str, np.ndarray],
        yaw_deg: float,
    ) -> Dict[str, np.ndarray]:
        rotation = self.owner._rotation_about_display_z(yaw_deg)
        rotated = {}
        for camera_name, transform in transforms.items():
            translation = rotation @ transform[:3, 3]
            rotated[camera_name] = matrix_to_transform(transform[:3, :3], translation)
        return rotated

    def _canonicalize_display_transforms(
        self,
        transforms: Dict[str, np.ndarray],
    ) -> Dict[str, np.ndarray]:
        if not self.owner.live_alignment_display_axis_alignment:
            return transforms
        canonical = {}
        identity = np.eye(3, dtype=np.float64)
        for camera_name, transform in transforms.items():
            canonical[camera_name] = matrix_to_transform(identity, transform[:3, 3])
        return canonical

    def _find_dashboard_pose_sample(self, camera_name: str, stamp_ns: int):
        with self.owner.pose_history_lock:
            history = list(self.owner.pose_history.get(camera_name, []))
        if not history:
            return None
        before = None
        after = None
        for sample in history:
            if sample.stamp_ns <= stamp_ns:
                before = sample
            if sample.stamp_ns >= stamp_ns:
                after = sample
                break
        if before is not None and after is not None:
            span_ns = after.stamp_ns - before.stamp_ns
            if span_ns <= max(self.owner.live_alignment_dashboard_pose_max_age_ns, 1):
                return interpolate_pose_sample(before, after, stamp_ns)
        best_sample = min(history, key=lambda sample: abs(sample.stamp_ns - stamp_ns))
        if abs(best_sample.stamp_ns - stamp_ns) > self.owner.live_alignment_dashboard_pose_max_age_ns:
            return None
        return best_sample

    def _build_dashboard_world_anchor(
        self,
        camera_name: str,
        stamp_ns: int,
        display_transform: np.ndarray,
    ) -> Optional[np.ndarray]:
        pose_sample = self.owner._find_dashboard_pose_sample(camera_name, stamp_ns)
        if pose_sample is None:
            # No emit here: this now runs once per detection frame (~5Hz), and
            # a camera with no VIO yet would flood the log. The per-second
            # summary and the "waiting pose" status carry the signal instead.
            self.owner._set_alignment_debug(camera_name, stage="anchor_missing_pose")
            return None
        pose_transform = pose_sample.as_transform()
        if self.owner.live_alignment_anchor_rotation_mode == "none":
            display_translation = display_transform[:3, 3]
            pose_translation = np.array(pose_sample.position, dtype=np.float64)
            anchor_translation = display_translation - pose_translation
            return matrix_to_transform(np.eye(3, dtype=np.float64), anchor_translation)
        if self.owner.live_alignment_anchor_rotation_mode == "yaw":
            target_yaw = math.atan2(
                float(display_transform[1, 0]),
                float(display_transform[0, 0]),
            )
            pose_yaw = math.atan2(
                float(pose_transform[1, 0]),
                float(pose_transform[0, 0]),
            )
            anchor_rotation = self.owner._rotation_about_display_z(math.degrees(target_yaw - pose_yaw))
            display_translation = display_transform[:3, 3]
            pose_translation = pose_transform[:3, 3]
            anchor_translation = display_translation - anchor_rotation @ pose_translation
            return matrix_to_transform(anchor_rotation, anchor_translation)
        return display_transform @ invert_transform(pose_transform)
