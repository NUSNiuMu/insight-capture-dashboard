"""Read-only pose, alignment, camera, and settings payload construction."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import quote

import numpy as np

from camera_setup import AVAILABLE_AVATAR_MODELS

from .models import CameraFrame


class PayloadBuilder:
    def __init__(self, owner) -> None:
        self.owner = owner

    def build_pose_payload(self) -> Dict[str, object]:
        now = time.monotonic()
        poses = []
        hand_entries = []  # payload dicts for visible hands
        with self.owner.pose_lock:
            for pose in self.owner.poses:
                transformed = self.owner.transformed_pose_sample(pose.name)
                raw_sample = self.owner.latest_pose_sample.get(pose.name)
                visible = raw_sample is not None and (self.owner.fake_pose or (now - self.owner.last_pose_received_time[pose.name]) <= self.owner.pose_timeout_sec)
                if transformed is None:
                    position = [0.0, 0.0, 0.0]
                    quaternion = [0.0, 0.0, 0.0, 1.0]
                else:
                    # Rounded to 0.01mm (position) / 1e-5 (quaternion, unitless) --
                    # this stream broadcasts at ~20Hz with a full trace history
                    # (up to max_points) resent every tick, so untruncated
                    # float64 repr (~17 sig figs) was bloating each message to
                    # ~60KB and both server-side json.dumps and client-side
                    # parse/render of that at 20Hz was the actual source of
                    # the trajectory lag -- far beyond what this visualization
                    # needs precision-wise.
                    position = [round(float(value), 5) for value in transformed.position]
                    quaternion = [round(float(value), 5) for value in transformed.orientation_xyzw]
                trace_points = self.owner.transformed_trace(pose.name)
                # np.round over the whole trace in C instead of a 300-iteration
                # Python loop -- measured ~2x faster, and less GIL hold time
                # per broadcast tick.
                trace = np.round(np.asarray(trace_points, dtype=np.float64), 4).tolist() if trace_points else []
                entry = {
                    "name": pose.name,
                    "role": pose.teleop_role,
                    "visible": visible,
                    "position": position,
                    "quaternion_xyzw": quaternion,
                    "trace": trace,
                    "avatar_model": pose.avatar_model,
                    "avatar_scale": pose.avatar_scale,
                    "avatar_rotation_deg_xyz": [float(value) for value in pose.avatar_rotation_deg_xyz],
                    "avatar_offset_xyz": [float(value) for value in pose.avatar_offset_xyz],
                    "gripper_opening": self.owner.gripper_opening_percent(pose.name),
                }
                poses.append(entry)
                if transformed is not None and visible and pose.teleop_role in ("left_hand", "right_hand"):
                    hand_entries.append(entry)
        # Stick-figure extra: the latest normalized 21-point hand shape (see
        # hand_landmarks_for_role; None until a HandEngine camera has
        # detected that hand), same dashboard frame as `position`.
        for entry in hand_entries:
            entry["hand_landmarks"] = self.owner.hand_landmarks_for_role(entry["role"])
        return {
            "type": "pose_update",
            "timestamp_ms": int(time.time() * 1000),
            "fake_pose": self.owner.fake_pose,
            "playback_mode": self.owner._playback_mode,
            "stick_figure_mode": bool(self.owner.stick_figure_mode),
            "alignment": self.owner.build_alignment_payload(),
            "poses": poses,
        }

    def build_alignment_payload(self) -> Dict[str, object]:
        target_camera = getattr(self.owner, "live_alignment_target_camera", None)
        inlier_counts = getattr(self.owner, "live_alignment_inlier_counts", {})
        return {
            "available": bool(self.owner.live_alignment_available and not self.owner.fake_pose),
            "active": bool(self.owner.live_alignment_active),
            "status_text": self.owner.alignment_status_text(),
            "lock_on_first_solution": bool(self.owner.live_alignment_lock_on_first_solution),
            "required_samples": int(self.owner.live_alignment_required_samples),
            "visible_cameras": int(getattr(self.owner, "live_alignment_visible_cameras", 0)),
            "camera_count": len(self.owner.cameras),
            "inlier_count": int(0 if target_camera is None else inlier_counts.get(target_camera, 0)),
            "last_status": str(getattr(self.owner, "live_alignment_last_status", "")),
            "has_solution": bool(self.owner.world_to_reference),
            "camera_names": [camera.name for camera in self.owner.cameras],
        }

    def build_camera_payload(self) -> Dict[str, object]:
        now = time.monotonic()
        cameras = []
        with self.owner.camera_frame_lock:
            for camera in self.owner.cameras:
                frame = self.owner.latest_camera_frames.get(camera.name)
                frame_times = list(self.owner.camera_frame_times.get(camera.name, []))
                recent_times = [item for item in frame_times if now - item <= 2.0]
                fps = 0.0
                if len(recent_times) >= 2:
                    span = max(recent_times[-1] - recent_times[0], 1e-6)
                    fps = (len(recent_times) - 1) / span
                stale = frame is None or (now - frame.received_monotonic) > self.owner.camera_stale_timeout_sec
                cameras.append(
                    {
                        "name": camera.name,
                        "label": camera.label,
                        "topic": camera.topic,
                        "type": camera.topic_type,
                        "visible": frame is not None,
                        "stale": stale,
                        "stamp_ns": 0 if frame is None else frame.stamp_ns,
                        "age_ms": None if frame is None else (now - frame.received_monotonic) * 1000.0,
                        "fps": fps,
                        "width": 0 if frame is None else frame.width,
                        "height": 0 if frame is None else frame.height,
                        "version": 0 if frame is None else frame.version,
                        "frame_url": f"/api/cameras/{quote(camera.name, safe='')}/frame",
                        "webrtc_available": self.owner._webrtc_available_cached,
                        "webrtc_port": self.owner.webrtc_port,
                        "rotation_deg": camera.rotation_deg,
                        "row": camera.row,
                        "column": camera.column,
                        "row_span": camera.row_span,
                        "column_span": camera.column_span,
                    }
                )
        return {
            "type": "camera_update",
            "timestamp_ms": int(time.time() * 1000),
            "cameras": cameras,
        }

    def latest_camera_frame(self, camera_name: str) -> Optional[CameraFrame]:
        with self.owner.camera_frame_lock:
            return self.owner.latest_camera_frames.get(camera_name)

    def model_asset_url(self, avatar_model: Optional[str]) -> Optional[str]:
        if not avatar_model:
            return None
        return f"/asset?path={quote(avatar_model, safe='')}"

    def build_settings_payload(self) -> Dict[str, object]:
        hand_cameras = set(getattr(self.owner, "gripper_calibrations", {}).keys())
        poses = []
        for pose in self.owner.poses:
            model_name = Path(pose.avatar_model).name if pose.avatar_model else None
            entry = {
                "name": pose.name,
                "role": pose.teleop_role,
                "avatar_model": model_name,
            }
            if pose.name in hand_cameras:
                entry["gripper_tracking_available"] = True
                entry["gripper_tracking_enabled"] = pose.name in self.owner.gripper_tracking_cameras
            if pose.name in getattr(self.owner, "hand_overlay_available", set()):
                entry["hand_overlay_available"] = True
                entry["hand_overlay_enabled"] = pose.name in self.owner.hand_overlay_enabled
            poses.append(entry)
        return {
            "poses": poses,
            "available_models": AVAILABLE_AVATAR_MODELS,
            "stick_figure_mode": bool(self.owner.stick_figure_mode),
        }
