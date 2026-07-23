"""Human-readable alignment status, logs, and result reports."""

from __future__ import annotations

import math
import time
from typing import Dict, Optional

import numpy as np

from .state import DetectionSample


class AlignmentDiagnostics:
    def __init__(self, owner) -> None:
        self.owner = owner

    def _format_transform_summary(self, transform: np.ndarray) -> str:
        translation = transform[:3, 3]
        trace = float(np.trace(transform[:3, :3]))
        cos_theta = max(-1.0, min(1.0, (trace - 1.0) * 0.5))
        rotation_angle_deg = math.degrees(math.acos(cos_theta))
        return (
            f"xyz=({translation[0]:.3f},{translation[1]:.3f},{translation[2]:.3f}) "
            f"rot={rotation_angle_deg:.1f}deg"
        )

    def _log_live_alignment_status(self, force: bool = False) -> None:
        status = self.owner.alignment_status_text()
        if not force and status == self.owner.live_alignment_logged_status:
            return
        self.owner.live_alignment_logged_status = status
        self.owner._emit_alignment_log(status)

    def _log_live_alignment_summary(self) -> None:
        now = time.monotonic()
        if self.owner.live_alignment_last_summary_time > 0.0 and (now - self.owner.live_alignment_last_summary_time) < 1.0:
            return
        self.owner.live_alignment_last_summary_time = now
        parts = []
        seen = []
        usable = []
        missing_images = []
        detection_ok = []
        pnp_failed = []
        pending_parts = []
        for camera in self.owner.cameras:
            has_image = self.owner.live_alignment_latest_image_stamp_ns[camera.name] > 0
            tag_count = self.owner.live_alignment_last_tag_count.get(camera.name, 0)
            state = self.owner.live_alignment_debug_state.get(camera.name, {})
            stage = str(state.get("stage", "-"))
            pending_parts.append(f"{camera.name}={state.get('pending', 0)}")
            if stage == "detection_ok":
                detection_ok.append(camera.name)
            elif stage == "pose_board_failed":
                pnp_failed.append(camera.name)
            if not has_image:
                missing_images.append(camera.name)
                parts.append(f"{camera.name}=no_img")
                continue
            if tag_count > 0:
                seen.append(camera.name)
            if tag_count >= self.owner.live_alignment_min_detected_tags:
                usable.append(camera.name)
            parts.append(f"{camera.name}={tag_count}")
        seen_text = ",".join(seen) if seen else "none"
        usable_text = ",".join(usable) if usable else "none"
        missing_text = ""
        if missing_images:
            missing_text = f" | missing_img={','.join(missing_images)}"
        stage_text = (
            f" | ok={','.join(detection_ok) if detection_ok else 'none'}"
            f" pnp_fail={','.join(pnp_failed) if pnp_failed else 'none'}"
            f" pending={' '.join(pending_parts)}"
        )
        self.owner._emit_alignment_log(
            f"tags {' '.join(parts)} | seen={seen_text} | usable={usable_text}{missing_text}{stage_text} | {self.owner.alignment_status_text()}"
        )

    def _emit_alignment_log(self, message: str) -> None:
        text = f"[alignment] {message}"
        print(text, flush=True)

    def _reset_live_alignment_debug_state(self) -> None:
        self.owner.live_alignment_debug_state = {
            camera.name: {
                "stage": "idle",
                "tags": 0,
                "pending": 0,
                "latency_ms": "-",
            }
            for camera in self.owner.cameras
        }

    def _set_alignment_debug(self, camera_name: str, **updates: object) -> None:
        state = self.owner.live_alignment_debug_state.setdefault(camera_name, {})
        state.update(updates)

    def _reset_alignment_result_txt(self) -> None:
        try:
            self.owner.live_alignment_result_txt_path.parent.mkdir(parents=True, exist_ok=True)
            header = (
                "# Insight live alignment latest result\n"
                f"# file={self.owner.live_alignment_result_txt_path}\n"
                f"# alignment_frame={self.owner.live_alignment_frame}\n"
                "# board_origin=center\n"
                f"# method={self.owner.live_alignment_method}\n"
            )
            self.owner.live_alignment_result_txt_path.write_text(header, encoding="utf-8")
            self.owner._emit_alignment_log(f"result txt: {self.owner.live_alignment_result_txt_path}")
        except Exception as exc:
            self.owner._emit_alignment_log(f"result txt unavailable: {exc}")

    def _write_alignment_result_txt(
        self,
        detections: Dict[str, DetectionSample],
        raw_transforms: Dict[str, np.ndarray],
        display_camera_transforms: Dict[str, np.ndarray],
        trajectory_anchor_transforms: Dict[str, np.ndarray],
        anchor_quality: Optional[Dict[str, str]] = None,
    ) -> None:
        try:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            lines = [
                "# Insight live alignment latest result",
                f"time={timestamp}",
                f"alignment_frame={self.owner.live_alignment_frame}",
                "board_origin=center",
                f"status={self.owner.alignment_status_text()}",
                "",
            ]
            for camera in self.owner.cameras:
                raw_transform = raw_transforms.get(camera.name)
                display_transform = display_camera_transforms.get(camera.name)
                anchor_transform = trajectory_anchor_transforms.get(camera.name)
                detection = detections.get(camera.name)
                lines.append(f"[{camera.name}]")
                if detection is not None:
                    lines.append(f"detection_stamp_ns={detection.stamp_ns}")
                if raw_transform is None or display_transform is None or anchor_transform is None:
                    lines.append("transform=missing")
                    lines.append("")
                    continue
                raw_translation = raw_transform[:3, 3]
                display_translation = display_transform[:3, 3]
                anchor_translation = anchor_transform[:3, 3]
                lines.append(
                    f"optical_xyz_m=({raw_translation[0]:.6f}, {raw_translation[1]:.6f}, {raw_translation[2]:.6f})"
                )
                lines.append(
                    f"display_xyz_m=({display_translation[0]:.6f}, {display_translation[1]:.6f}, {display_translation[2]:.6f})"
                )
                lines.append(
                    f"anchor_xyz_m=({anchor_translation[0]:.6f}, {anchor_translation[1]:.6f}, {anchor_translation[2]:.6f})"
                )
                quality = (anchor_quality or {}).get(camera.name)
                if quality:
                    lines.append(f"anchor_rms={quality}")
                lines.append("")
            self.owner.live_alignment_result_txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception as exc:
            self.owner._emit_alignment_log(f"result txt write failed: {exc}")

    def alignment_status_text(self) -> str:
        if not self.owner.session_alignment_enabled:
            return "alignment disabled"
        if self.owner.live_alignment_active:
            if self.owner.live_alignment_last_status == "waiting-board":
                return f"Alignment ON | board {self.owner.live_alignment_visible_cameras}/{len(self.owner.cameras)}"
            if self.owner.live_alignment_last_status == "waiting-pose":
                return "Alignment ON | waiting pose"
            if self.owner.live_alignment_last_status == "collecting":
                target_camera = self.owner.live_alignment_target_camera
                done = 0 if target_camera is None else int(self.owner.live_alignment_inlier_counts.get(target_camera, 0))
                return f"Alignment ON | samples {done}/{self.owner.live_alignment_required_samples}"
            if self.owner.live_alignment_last_status == "tracking":
                return "Alignment ON | tracking"
            if self.owner.live_alignment_last_status == "unstable":
                return "Alignment ON | unstable (anchor spread too high)"
            return "Alignment ON"
        if not self.owner.world_to_reference:
            return "Alignment OFF"
        return "Alignment OFF | locked"
