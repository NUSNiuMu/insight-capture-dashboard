"""Persisted alignment transform serialization and reload."""

from __future__ import annotations

import json
import time
from typing import Dict, List, Optional

import numpy as np


class AlignmentPersistence:
    def __init__(self, owner) -> None:
        self.owner = owner

    def _serialize_transform(self, transform: np.ndarray) -> List[List[float]]:
        return [[float(value) for value in row] for row in transform.tolist()]

    def _deserialize_transform(self, value: object) -> Optional[np.ndarray]:
        try:
            matrix = np.array(value, dtype=np.float64)
        except Exception:
            return None
        if matrix.shape != (4, 4):
            return None
        return matrix

    def _persist_alignment_state(self) -> None:
        if not self.owner.session_alignment_enabled or not self.owner.world_to_reference:
            return
        try:
            transforms = {}
            lock = getattr(self.owner, "live_alignment_solution_lock", None)
            if lock is None:
                source_transforms = dict(self.owner.world_to_reference)
            else:
                with lock:
                    source_transforms = dict(self.owner.world_to_reference)
            for camera_name, transform in source_transforms.items():
                transforms[camera_name] = self.owner._serialize_transform(transform)
            payload = {
                "version": 1,
                "saved_at_epoch_s": time.time(),
                "saved_at_local": time.strftime("%Y-%m-%d %H:%M:%S"),
                "alignment_frame": self.owner.live_alignment_frame,
                "reference_camera": self.owner.reference_camera,
                "status": self.owner.alignment_status_text(),
                "display_axis_alignment": bool(self.owner.live_alignment_display_axis_alignment),
                "anchor_rotation_mode": self.owner.live_alignment_anchor_rotation_mode,
                "dashboard_horizontal_yaw_mode": self.owner.live_alignment_dashboard_horizontal_yaw_mode,
                "dashboard_horizontal_yaw_deg": float(self.owner.live_alignment_dashboard_horizontal_yaw_deg),
                "camera_names": [camera.name for camera in self.owner.cameras],
                "world_to_reference": transforms,
            }
            self.owner.live_alignment_state_path.parent.mkdir(parents=True, exist_ok=True)
            self.owner.live_alignment_state_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
                encoding="utf-8",
            )
            self.owner._emit_alignment_log(f"alignment state saved: {self.owner.live_alignment_state_path}")
        except Exception as exc:
            self.owner._emit_alignment_log(f"alignment state save failed: {exc}")

    def _load_persisted_alignment_state(self) -> None:
        if not self.owner.session_alignment_enabled:
            return
        if not self.owner.live_alignment_state_path.exists():
            return
        try:
            payload = json.loads(self.owner.live_alignment_state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.owner._emit_alignment_log(f"alignment state load failed: {exc}")
            return
        transforms_payload = payload.get("world_to_reference")
        if not isinstance(transforms_payload, dict):
            return
        if str(payload.get("alignment_frame") or self.owner.live_alignment_frame) != self.owner.live_alignment_frame:
            return
        available_cameras = {camera.name for camera in self.owner.cameras}
        loaded_transforms: Dict[str, np.ndarray] = {}
        for camera_name, transform_value in transforms_payload.items():
            if camera_name not in available_cameras:
                continue
            matrix = self.owner._deserialize_transform(transform_value)
            if matrix is None:
                continue
            loaded_transforms[camera_name] = matrix
        if not loaded_transforms:
            return
        lock = getattr(self.owner, "live_alignment_solution_lock", None)
        if lock is None:
            self.owner.world_to_reference = loaded_transforms
        else:
            with lock:
                self.owner.world_to_reference = loaded_transforms
        self.owner.live_alignment_last_status = "locked"
        self.owner.live_alignment_last_transform_summary = {
            camera_name: self.owner._format_transform_summary(transform)
            for camera_name, transform in loaded_transforms.items()
        }
        self.owner._refresh_transformed_poses()
        self.owner.invalidate_trace_snapshots()
        self.owner._emit_alignment_log(f"alignment state loaded: {self.owner.live_alignment_state_path}")
