"""Read-only pose, camera, and settings payload construction."""

from __future__ import annotations

import time
from typing import Dict, Optional
from urllib.parse import quote

from insight_capture.core.localization_settings import load_gripper_mask_height_ratio

import numpy as np

from insight_capture.core.models import CameraFrame


class PayloadBuilder:
    def __init__(self, owner) -> None:
        self.owner = owner

    def build_pose_payload(
        self, trace_cursor: Optional[Dict[str, object]] = None
    ) -> Dict[str, object]:
        now = time.monotonic()
        poses = []
        hand_entries = []  # payload dicts for visible hands
        cursor_sequences = {} if trace_cursor is None else trace_cursor.get("sequences", {})
        if not isinstance(cursor_sequences, dict):
            cursor_sequences = {}
        with self.owner.pose_lock:
            trace_generation = int(self.owner.trace_generation)
            cursor_generation = (
                -1 if trace_cursor is None else int(trace_cursor.get("generation", -1))
            )
            force_trace_snapshot = cursor_generation != trace_generation
            for pose in self.owner.poses:
                raw_sample = self.owner.latest_pose_sample.get(pose.name)
                visible = raw_sample is not None and (self.owner.fake_pose or (now - self.owner.last_pose_received_time[pose.name]) <= self.owner.pose_timeout_sec)
                if raw_sample is None:
                    position = [0.0, 0.0, 0.0]
                    quaternion = [0.0, 0.0, 0.0, 1.0]
                else:
                    # Truncate visualization precision to keep broadcasts compact.
                    position = [round(float(value), 5) for value in raw_sample.position]
                    quaternion = [round(float(value), 5) for value in raw_sample.orientation_xyzw]
                raw_trace = list(self.owner.raw_traces[pose.name])
                trace_sequences = list(self.owner.raw_trace_sequences[pose.name])
                latest_trace_sequence = int(self.owner.trace_sequences[pose.name])
                first_trace_sequence = (
                    trace_sequences[0] if trace_sequences else latest_trace_sequence + 1
                )
                cursor_sequence = int(cursor_sequences.get(pose.name, -1))
                trace_mode = "delta"
                if (
                    force_trace_snapshot
                    or cursor_sequence < first_trace_sequence - 1
                    or cursor_sequence > latest_trace_sequence
                ):
                    trace_mode = "snapshot"
                    selected_trace = raw_trace
                    selected_sequences = trace_sequences
                else:
                    first_new_index = len(trace_sequences)
                    for index, sequence in enumerate(trace_sequences):
                        if sequence > cursor_sequence:
                            first_new_index = index
                            break
                    selected_trace = raw_trace[first_new_index:]
                    selected_sequences = trace_sequences[first_new_index:]
                if len(selected_trace) > 32:
                    trace_points = np.round(
                        np.asarray(selected_trace, dtype=np.float64), 4
                    ).tolist()
                else:
                    trace_points = [
                        [round(float(value), 4) for value in point]
                        for point in selected_trace
                    ]
                entry = {
                    "name": pose.name,
                    "role": pose.teleop_role,
                    "visible": visible,
                    "position": position,
                    "quaternion_xyzw": quaternion,
                    "trace_update": {
                        "mode": trace_mode,
                        "generation": trace_generation,
                        "from_seq": (
                            int(selected_sequences[0])
                            if selected_sequences
                            else latest_trace_sequence + 1
                        ),
                        "to_seq": latest_trace_sequence,
                        "drop_before_seq": int(first_trace_sequence),
                        "points": trace_points,
                    },
                    "avatar_model": pose.avatar_model,
                    "avatar_scale": pose.avatar_scale,
                    "avatar_rotation_deg_xyz": [
                        float(value) for value in pose.avatar_rotation_deg_xyz
                    ],
                    "avatar_offset_xyz": [
                        float(value) for value in pose.avatar_offset_xyz
                    ],
                    "gripper_opening": self.owner.gripper_opening_percent(pose.name),
                }
                poses.append(entry)
                if raw_sample is not None and visible and pose.teleop_role in ("left_hand", "right_hand"):
                    hand_entries.append(entry)
        return {
            "type": "pose_update",
            "display_fps_limit": self.owner.display_fps_limit,
            "trace_capacity": self.owner.max_points,
            "trace_generation": trace_generation,
            "poses": poses,
        }

    def build_camera_payload(self) -> Dict[str, object]:
        now = time.monotonic()
        cameras = []
        with self.owner._webrtc_metrics_lock:
            main_metrics = {
                name: dict(metrics)
                for name, metrics in self.owner._webrtc_main_metrics.items()
            }
            worker_stats = {
                name: dict(metrics)
                for name, metrics in self.owner._webrtc_worker_stats.items()
                if isinstance(metrics, dict)
            }
            browser_stats = {
                name: dict(metrics)
                for name, metrics in self.owner._webrtc_browser_stats.items()
                if isinstance(metrics, dict)
            }
        with self.owner.camera_frame_lock:
            for camera in self.owner.cameras:
                frame = self.owner.latest_camera_frames.get(camera.name)
                with self.owner.camera_input_lock:
                    input_times = list(
                        self.owner.camera_input_times.get(camera.name, [])
                    )
                recent_input_times = [
                    item for item in input_times if now - item <= 2.0
                ]
                input_fps = 0.0
                if len(recent_input_times) >= 2:
                    input_span = max(
                        recent_input_times[-1] - recent_input_times[0], 1e-6
                    )
                    input_fps = (
                        len(recent_input_times) - 1
                    ) / input_span
                frame_times = list(self.owner.camera_frame_times.get(camera.name, []))
                recent_times = [item for item in frame_times if now - item <= 2.0]
                fps = 0.0
                if len(recent_times) >= 2:
                    span = max(recent_times[-1] - recent_times[0], 1e-6)
                    fps = (len(recent_times) - 1) / span
                input_age = None if not input_times else now - input_times[-1]
                stale = input_age is None or input_age > self.owner.camera_stale_timeout_sec
                browser = browser_stats.get(camera.name, {})
                browser_age_sec = now - float(
                    browser.pop("updated_monotonic", 0.0)
                )
                if browser_age_sec > 5.0:
                    browser = {}
                elif browser:
                    browser["age_sec"] = max(0.0, browser_age_sec)
                cameras.append(
                    {
                        "name": camera.name,
                        "label": camera.label,
                        "topic": camera.topic,
                        "visible": frame is not None,
                        "stale": stale,
                        "input_age_sec": input_age,
                        "fps": fps,
                        "webrtc_stats": {
                            "input_fps": input_fps,
                            "processed_fps": fps,
                            "main": main_metrics.get(camera.name, {}),
                            "worker": worker_stats.get(camera.name, {}),
                            "browser": browser,
                        },
                        "width": 0 if frame is None else frame.width,
                        "height": 0 if frame is None else frame.height,
                        "version": 0 if frame is None else frame.version,
                        "frame_url": f"/api/cameras/{quote(camera.name, safe='')}/frame",
                        "webrtc_available": self.owner._webrtc_available_cached,
                        "webrtc_port": self.owner.webrtc_port,
                        "rotation_deg": camera.rotation_deg,
                        "row": camera.row,
                        "column": camera.column,
                    }
                )
        return {
            "type": "camera_update",
            "runtime": self.owner.preview_status(),
            "cameras": cameras,
        }

    def latest_camera_frame(self, camera_name: str) -> Optional[CameraFrame]:
        with self.owner.camera_frame_lock:
            return self.owner.latest_camera_frames.get(camera_name)

    def model_asset_url(self, avatar_model: Optional[str]) -> Optional[str]:
        if not avatar_model:
            return None
        asset_path = (self.owner.project_root / avatar_model).resolve()
        try:
            version = asset_path.stat().st_mtime_ns
        except OSError:
            version = 0
        return f"/asset?path={quote(avatar_model, safe='')}&v={version}"

    def build_settings_payload(self) -> Dict[str, object]:
        hand_cameras = {
            name
            for name, calibration in getattr(
                self.owner, "gripper_calibrations", {}
            ).items()
            if calibration.is_valid
        }
        poses = []
        for pose in self.owner.poses:
            entry = {
                "name": pose.name,
                "role": pose.teleop_role,
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
            "insight3_gripper_mask_height_ratio": load_gripper_mask_height_ratio(
                self.owner.post_processing_config_path
            ),
        }
