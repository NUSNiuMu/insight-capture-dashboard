"""Online alignment lifecycle, sample queues, and solution updates."""

from __future__ import annotations

import time

import cv2
import numpy as np

from session_alignment import average_transforms, matrix_to_transform

from .state import DetectionSample


class AlignmentLifecycle:
    def __init__(self, owner) -> None:
        self.owner = owner

    def start_live_alignment(self) -> str:
        if not self.owner.live_alignment_available:
            self.owner.live_alignment_last_status = "live alignment unavailable: OpenCV aruco or session alignment config missing"
            return self.owner.live_alignment_last_status
        self.owner._set_live_alignment_timer_enabled(True)
        self.owner.live_alignment_active = True
        # Do NOT clear world_to_reference here — existing cameras keep their positions.
        # Each camera's transform is overwritten only when that camera successfully calibrates.
        self.owner.live_alignment_last_status = "alignment on"
        self.owner.live_alignment_last_signature = None
        self.owner.live_alignment_latest_detection = {camera.name: None for camera in self.owner.cameras}
        self.owner.live_alignment_detection_buffer = {camera.name: [] for camera in self.owner.cameras}
        self.owner.live_alignment_pending_images = {camera.name: [] for camera in self.owner.cameras}
        self.owner.live_alignment_processed_stamp_ns = {camera.name: -1 for camera in self.owner.cameras}
        self.owner.live_alignment_samples_by_camera = {camera.name: [] for camera in self.owner.cameras}
        self.owner.live_alignment_visible_cameras = 0
        self.owner.live_alignment_inlier_counts = {}
        self.owner.live_alignment_target_camera = None
        self.owner.live_alignment_logged_status = None
        self.owner.live_alignment_last_sync_span_ms = 0.0
        self.owner.live_alignment_last_transform_summary = {}
        self.owner.live_alignment_last_raw_transform_summary = {}
        self.owner.live_alignment_last_anchor_summary = {}
        self.owner.live_alignment_last_tag_count = {camera.name: 0 for camera in self.owner.cameras}
        self.owner.live_alignment_last_summary_time = 0.0
        self.owner._reset_live_alignment_debug_state()
        self.owner._reset_alignment_result_txt()
        self.owner._log_live_alignment_status(force=True)
        self.owner._emit_alignment_log(
            "config "
            f"method={self.owner.live_alignment_method} stream={self.owner.live_alignment_image_stream} "
            f"dict={self.owner.live_alignment_dictionary_name} scale={self.owner.live_alignment_image_scale:.2f} "
            f"processing_hz={self.owner.live_alignment_processing_hz:.1f} "
            f"min_tags={self.owner.live_alignment_min_detected_tags} required={self.owner.live_alignment_required_samples} "
            f"display_axis_alignment={'on' if self.owner.live_alignment_display_axis_alignment else 'off'} "
            f"lock_on_first_solution={'on' if self.owner.live_alignment_lock_on_first_solution else 'off'} "
            f"reset_traces_on_lock={'on' if self.owner.live_alignment_reset_traces_on_lock else 'off'} "
            f"anchor_rotation_mode={self.owner.live_alignment_anchor_rotation_mode} "
            f"horizontal_yaw_mode={self.owner.live_alignment_dashboard_horizontal_yaw_mode} "
            f"horizontal_yaw_deg={self.owner.live_alignment_dashboard_horizontal_yaw_deg:.1f}"
        )
        for camera in self.owner.cameras:
            self.owner._emit_alignment_log(
                f"subscribe {camera.name}: image={self.owner.live_alignment_topic_by_camera.get(camera.name, '-')} "
                f"info={camera.camera_info_topic} pose=used_for_board_anchor"
            )
        return self.owner.alignment_status_text()

    def stop_live_alignment(self) -> str:
        self.owner._set_live_alignment_timer_enabled(False)
        self.owner.live_alignment_active = False
        self.owner.live_alignment_last_status = "alignment paused"
        self.owner._log_live_alignment_status(force=True)
        return self.owner.alignment_status_text()

    def _lock_live_alignment_solution(self) -> None:
        self.owner._set_live_alignment_timer_enabled(False)
        self.owner.live_alignment_active = False
        self.owner.live_alignment_last_status = "locked"
        self.owner.live_alignment_last_signature = None
        self.owner.live_alignment_latest_detection = {camera.name: None for camera in self.owner.cameras}
        self.owner.live_alignment_detection_buffer = {camera.name: [] for camera in self.owner.cameras}
        self.owner.live_alignment_pending_images = {camera.name: [] for camera in self.owner.cameras}
        self.owner.live_alignment_processed_stamp_ns = {camera.name: -1 for camera in self.owner.cameras}
        self.owner.live_alignment_samples_by_camera = {camera.name: [] for camera in self.owner.cameras}
        self.owner.live_alignment_target_camera = None
        if self.owner.live_alignment_reset_traces_on_lock:
            for pose in self.owner.poses:
                raw_trace = self.owner.raw_traces.get(pose.name)
                if not raw_trace:
                    continue
                last_point = raw_trace[-1]
                # In-place clear+append keeps the container (a bounded deque
                # owned by the dashboard node) rather than replacing it with
                # a plain unbounded list.
                raw_trace.clear()
                raw_trace.append(last_point)
                trace_sequences = self.owner.raw_trace_sequences.get(pose.name)
                if trace_sequences:
                    last_sequence = trace_sequences[-1]
                    trace_sequences.clear()
                    trace_sequences.append(last_sequence)
                self.owner.latest_pose[pose.name] = self.owner._transform_pose_point(pose.name, last_point)
            self.owner.invalidate_trace_snapshots()
        self.owner._persist_alignment_state()
        self.owner._log_live_alignment_status(force=True)

    def _set_live_alignment_timer_enabled(self, enabled: bool) -> None:
        timer = getattr(self.owner, "live_alignment_timer", None)
        if timer is None:
            return
        try:
            if enabled:
                timer.reset()
            else:
                timer.cancel()
        except Exception:
            pass

    def _process_live_alignment(self) -> None:
        if not self.owner.live_alignment_active:
            return
        now_monotonic_ns = time.monotonic_ns()
        for camera in self.owner.cameras:
            lock = getattr(self.owner, "live_alignment_image_lock", None)
            if lock is None:
                pending = list(self.owner.live_alignment_pending_images[camera.name])
            else:
                with lock:
                    pending = list(self.owner.live_alignment_pending_images[camera.name])

            if not pending:
                self.owner.live_alignment_last_tag_count[camera.name] = 0
                self.owner.live_alignment_latest_detection[camera.name] = None
                stage = "no_image" if self.owner.live_alignment_latest_image_stamp_ns[camera.name] <= 0 else "waiting_image"
                self.owner._set_alignment_debug(camera.name, stage=stage, tags=0, pending=0)
                continue
            self.owner._prune_pending_alignment_images(camera.name, now_monotonic_ns)
            lock = getattr(self.owner, "live_alignment_image_lock", None)
            if lock is None:
                pending = list(self.owner.live_alignment_pending_images[camera.name])
            else:
                with lock:
                    pending = list(self.owner.live_alignment_pending_images[camera.name])
            self.owner._set_alignment_debug(camera.name, pending=len(pending))
            for stamp_ns, received_monotonic_ns, image in pending:
                if stamp_ns <= self.owner.live_alignment_processed_stamp_ns[camera.name]:
                    self.owner._drop_pending_alignment_images(camera.name, stamp_ns)
                    continue
                self.owner._set_alignment_debug(
                    camera.name,
                    latency_ms=f"{(now_monotonic_ns - received_monotonic_ns) / 1_000_000.0:.1f}",
                )
                processed = self.owner._process_live_alignment_image(camera.name, stamp_ns, image)
                if processed:
                    self.owner.live_alignment_processed_stamp_ns[camera.name] = stamp_ns
                    self.owner._drop_pending_alignment_images(camera.name, stamp_ns)
                break
        self.owner._log_live_alignment_summary()

    def _process_live_alignment_image(self, camera_name: str, stamp_ns: int, image_bgr: np.ndarray) -> bool:
        if self.owner.live_alignment_detector is not None:
            corners, ids, _ = self.owner.live_alignment_detector.detectMarkers(image_bgr)
        else:
            corners, ids, _ = cv2.aruco.detectMarkers(image_bgr, self.owner.live_alignment_aruco_dict)
        if ids is None or len(ids) < self.owner.live_alignment_min_detected_tags:
            self.owner.live_alignment_last_tag_count[camera_name] = 0 if ids is None else int(len(ids))
            self.owner.live_alignment_latest_detection[camera_name] = None
            self.owner._set_alignment_debug(
                camera_name,
                stage="tags_low",
                tags=self.owner.live_alignment_last_tag_count[camera_name],
            )
            return True
        self.owner.live_alignment_last_tag_count[camera_name] = int(len(ids))
        self.owner._set_alignment_debug(camera_name, stage="tags_ok", tags=int(len(ids)))

        camera_matrix = self.owner.live_alignment_camera_matrix[camera_name]
        dist_coeffs = self.owner.live_alignment_dist_coeffs[camera_name]
        if camera_matrix is None or dist_coeffs is None:
            self.owner.live_alignment_latest_detection[camera_name] = None
            self.owner._set_alignment_debug(camera_name, stage="missing_camera_info")
            return False
        detection_image = image_bgr
        detection_camera_matrix = camera_matrix
        if self.owner.live_alignment_image_scale != 1.0:
            height, width = image_bgr.shape[:2]
            scaled_width = max(1, int(round(width * self.owner.live_alignment_image_scale)))
            scaled_height = max(1, int(round(height * self.owner.live_alignment_image_scale)))
            detection_image = cv2.resize(
                image_bgr,
                (scaled_width, scaled_height),
                interpolation=cv2.INTER_AREA,
            )
            detection_camera_matrix = camera_matrix.copy()
            detection_camera_matrix[0, 0] *= self.owner.live_alignment_image_scale
            detection_camera_matrix[1, 1] *= self.owner.live_alignment_image_scale
            detection_camera_matrix[0, 2] *= self.owner.live_alignment_image_scale
            detection_camera_matrix[1, 2] *= self.owner.live_alignment_image_scale
        if detection_image is not image_bgr:
            if self.owner.live_alignment_detector is not None:
                corners, ids, _ = self.owner.live_alignment_detector.detectMarkers(detection_image)
            else:
                corners, ids, _ = cv2.aruco.detectMarkers(detection_image, self.owner.live_alignment_aruco_dict)
            if ids is None or len(ids) < self.owner.live_alignment_min_detected_tags:
                self.owner.live_alignment_last_tag_count[camera_name] = 0 if ids is None else int(len(ids))
                self.owner.live_alignment_latest_detection[camera_name] = None
                self.owner._set_alignment_debug(
                    camera_name,
                    stage="tags_low",
                    tags=self.owner.live_alignment_last_tag_count[camera_name],
                )
                return True
        pose_result = self.owner._solve_board_pose(corners, ids, detection_camera_matrix, dist_coeffs)
        if pose_result is None:
            self.owner.live_alignment_latest_detection[camera_name] = None
            self.owner._set_alignment_debug(camera_name, stage="pose_board_failed")
            return True
        rvec, tvec, reproj_rms_px = pose_result
        if reproj_rms_px is not None:
            self.owner._set_alignment_debug(camera_name, reproj_px=f"{reproj_rms_px:.2f}")
            if reproj_rms_px > self.owner.live_alignment_max_reprojection_error_px:
                self.owner.live_alignment_latest_detection[camera_name] = None
                self.owner._set_alignment_debug(camera_name, stage="reproj_high")
                return True
        rotation, _ = cv2.Rodrigues(rvec)
        t_camera_board_corner = matrix_to_transform(rotation, tvec.reshape(3))
        t_camera_board = t_camera_board_corner @ self.owner.live_alignment_board_center_offset
        self.owner.live_alignment_latest_detection[camera_name] = DetectionSample(
            stamp_ns=stamp_ns,
            marker_transform=t_camera_board,
        )
        self.owner._store_live_alignment_detection(camera_name, self.owner.live_alignment_latest_detection[camera_name])
        self.owner._set_alignment_debug(
            camera_name,
            stage="detection_ok",
            tags=self.owner.live_alignment_last_tag_count[camera_name],
        )
        self.owner._update_live_alignment_solution(camera_name)
        return True

    def _store_live_alignment_detection(self, camera_name: str, sample: DetectionSample) -> None:
        lock = getattr(self.owner, "live_alignment_solution_lock", None)
        if lock is None:
            self.owner._store_live_alignment_detection_unlocked(camera_name, sample)
            return
        with lock:
            self.owner._store_live_alignment_detection_unlocked(camera_name, sample)

    def _store_live_alignment_detection_unlocked(self, camera_name: str, sample: DetectionSample) -> None:
        self.owner.live_alignment_latest_detection[camera_name] = sample
        buffer = self.owner.live_alignment_detection_buffer[camera_name]
        buffer.append(sample)
        newest_stamp_ns = sample.stamp_ns
        min_stamp_ns = newest_stamp_ns - self.owner.live_alignment_detection_max_age_ns
        buffer[:] = [item for item in buffer if item.stamp_ns >= min_stamp_ns]
        if len(buffer) > self.owner.live_alignment_detection_buffer_limit:
            del buffer[: len(buffer) - self.owner.live_alignment_detection_buffer_limit]

    def _drop_pending_alignment_images(self, camera_name: str, through_stamp_ns: int) -> None:
        lock = getattr(self.owner, "live_alignment_image_lock", None)
        if lock is None:
            self.owner.live_alignment_pending_images[camera_name] = [
                item for item in self.owner.live_alignment_pending_images[camera_name]
                if item[0] > through_stamp_ns
            ]
            return
        with lock:
            self.owner.live_alignment_pending_images[camera_name] = [
                item for item in self.owner.live_alignment_pending_images[camera_name]
                if item[0] > through_stamp_ns
            ]

    def _prune_pending_alignment_images(self, camera_name: str, now_monotonic_ns: int) -> None:
        min_received_ns = now_monotonic_ns - self.owner.live_alignment_pending_max_age_ns
        lock = getattr(self.owner, "live_alignment_image_lock", None)
        if lock is None:
            self.owner.live_alignment_pending_images[camera_name] = [
                item for item in self.owner.live_alignment_pending_images[camera_name]
                if item[1] >= min_received_ns
            ]
            if len(self.owner.live_alignment_pending_images[camera_name]) > self.owner.live_alignment_pending_image_limit:
                del self.owner.live_alignment_pending_images[camera_name][
                    : len(self.owner.live_alignment_pending_images[camera_name]) - self.owner.live_alignment_pending_image_limit
                ]
            return
        with lock:
            self.owner.live_alignment_pending_images[camera_name] = [
                item for item in self.owner.live_alignment_pending_images[camera_name]
                if item[1] >= min_received_ns
            ]
            if len(self.owner.live_alignment_pending_images[camera_name]) > self.owner.live_alignment_pending_image_limit:
                del self.owner.live_alignment_pending_images[camera_name][
                    : len(self.owner.live_alignment_pending_images[camera_name]) - self.owner.live_alignment_pending_image_limit
                ]

    def _update_live_alignment_solution(self, camera_name: str) -> None:
        detection = self.owner.live_alignment_latest_detection.get(camera_name)
        if detection is None:
            self.owner.live_alignment_last_status = "waiting-board"
            self.owner._log_live_alignment_status()
            return
        self.owner.live_alignment_visible_cameras = sum(
            1 for sample in self.owner.live_alignment_latest_detection.values() if sample is not None
        )
        target_camera = self.owner.live_alignment_target_camera
        if target_camera != camera_name:
            target_detection = None if target_camera is None else self.owner.live_alignment_latest_detection.get(target_camera)
            if target_detection is not None:
                return
            self.owner.live_alignment_target_camera = camera_name
            self.owner.live_alignment_samples_by_camera[camera_name] = []
            self.owner.live_alignment_inlier_counts[camera_name] = 0

        # Pair each detection with its interpolated VIO pose to form a stable anchor.
        board_to_camera = detection.marker_transform
        base_transform = self.owner._dashboard_transform_from_optical(board_to_camera)
        dashboard_yaw_deg = self.owner._dashboard_horizontal_yaw_deg_from_transforms({camera_name: base_transform})
        display_transform = self.owner._apply_dashboard_horizontal_yaw(
            {camera_name: base_transform},
            dashboard_yaw_deg,
        )[camera_name]
        display_transform = self.owner._canonicalize_display_transforms({camera_name: display_transform})[camera_name]
        anchor_candidate = self.owner._build_dashboard_world_anchor(camera_name, detection.stamp_ns, display_transform)

        samples = self.owner.live_alignment_samples_by_camera[camera_name]
        samples.append((detection.stamp_ns, board_to_camera, display_transform, anchor_candidate))
        if len(samples) > self.owner.live_alignment_window:
            del samples[: len(samples) - self.owner.live_alignment_window]

        anchored = [item for item in samples if item[3] is not None]
        if not anchored:
            self.owner.live_alignment_last_status = "waiting-pose"
            self.owner._log_live_alignment_status()
            return

        anchor_list = [item[3] for item in anchored]
        inlier_indices = self.owner._inlier_transform_indices(
            anchor_list,
            self.owner.live_alignment_anchor_max_translation_std_m,
            self.owner.live_alignment_anchor_max_rotation_std_deg,
        )
        self.owner.live_alignment_inlier_counts[camera_name] = len(inlier_indices)
        if len(inlier_indices) < self.owner.live_alignment_required_samples:
            self.owner.live_alignment_last_status = "collecting"
            self.owner._log_live_alignment_status()
            return

        selected = inlier_indices[-self.owner.live_alignment_required_samples :]
        selected_anchors = [anchor_list[index] for index in selected]
        anchor_transform = average_transforms(selected_anchors)
        if anchor_transform is None:
            return

        # Enforce an RMS ceiling because adaptive MAD alone accepts uniform noise.
        deviations = [self.owner._pose_delta_metrics(anchor_transform, anchor) for anchor in selected_anchors]
        anchor_translation_rms_m = float(np.sqrt(np.mean([d[0] ** 2 for d in deviations])))
        anchor_rotation_rms_deg = float(np.sqrt(np.mean([d[1] ** 2 for d in deviations])))
        quality_text = f"{anchor_translation_rms_m * 1000.0:.1f}mm/{anchor_rotation_rms_deg:.2f}deg"
        self.owner._set_alignment_debug(camera_name, anchor_rms=quality_text)
        if (
            anchor_translation_rms_m > self.owner.live_alignment_anchor_max_translation_std_m
            or anchor_rotation_rms_deg > self.owner.live_alignment_anchor_max_rotation_std_deg
        ):
            self.owner.live_alignment_last_status = "unstable"
            self.owner._log_live_alignment_status()
            return

        # Average matching board/display transforms for diagnostics.
        averaged_board_to_camera = average_transforms([anchored[index][1] for index in selected])
        averaged_display = average_transforms([anchored[index][2] for index in selected])
        if averaged_board_to_camera is None or averaged_display is None:
            return
        display_transform = averaged_display

        lock = getattr(self.owner, "live_alignment_solution_lock", None)
        if lock is None:
            self.owner.world_to_reference[camera_name] = anchor_transform
            self.owner.live_alignment_last_transform_summary[camera_name] = self.owner._format_transform_summary(display_transform)
            self.owner.live_alignment_last_raw_transform_summary[camera_name] = self.owner._format_transform_summary(averaged_board_to_camera)
            self.owner.live_alignment_last_anchor_summary[camera_name] = self.owner._format_transform_summary(anchor_transform)
        else:
            with lock:
                self.owner.world_to_reference[camera_name] = anchor_transform
                self.owner.live_alignment_last_transform_summary[camera_name] = self.owner._format_transform_summary(display_transform)
                self.owner.live_alignment_last_raw_transform_summary[camera_name] = self.owner._format_transform_summary(averaged_board_to_camera)
                self.owner.live_alignment_last_anchor_summary[camera_name] = self.owner._format_transform_summary(anchor_transform)

        self.owner._write_alignment_result_txt(
            {camera_name: detection},
            {camera_name: averaged_board_to_camera},
            {camera_name: display_transform},
            {camera_name: anchor_transform},
            anchor_quality={camera_name: quality_text},
        )
        self.owner._refresh_transformed_poses()
        self.owner.invalidate_trace_snapshots()
        self.owner.live_alignment_last_status = "tracking"
        self.owner._log_live_alignment_status()
        raw_translation = averaged_board_to_camera[:3, 3]
        display_translation = display_transform[:3, 3]
        anchor_translation = anchor_transform[:3, 3]
        self.owner._emit_alignment_log(
            f"CALIBRATED {camera_name} | samples={len(selected_anchors)}/{len(anchor_list)} "
            f"anchor_rms={quality_text} "
            f"board_to_camera=({raw_translation[0]:.3f}, {raw_translation[1]:.3f}, {raw_translation[2]:.3f})m "
            f"dashboard_position=({display_translation[0]:.3f}, {display_translation[1]:.3f}, {display_translation[2]:.3f})m "
            f"vio_to_board_anchor=({anchor_translation[0]:.3f}, {anchor_translation[1]:.3f}, {anchor_translation[2]:.3f})m"
        )
        if self.owner.live_alignment_lock_on_first_solution:
            self.owner._emit_alignment_log("calibration stopped after this camera; press Start Alignment again for another camera")
            self.owner._lock_live_alignment_solution()
        else:
            self.owner._persist_alignment_state()

    def _refresh_transformed_poses(self) -> None:
        for pose in self.owner.poses:
            raw_trace = self.owner.raw_traces[pose.name]
            if raw_trace:
                self.owner.latest_pose[pose.name] = self.owner._transform_pose_point(pose.name, raw_trace[-1])
