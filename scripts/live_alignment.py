import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
from sensor_msgs.msg import Image as RosImage

from session_alignment import (
    average_transforms,
    interpolate_pose_sample,
    invert_transform,
    matrix_to_transform,
    transform_point,
    transform_pose_sample,
)


@dataclass
class DetectionSample:
    stamp_ns: int
    marker_transform: np.ndarray


class LiveAlignmentMixin:
    def _configure_live_alignment(self, raw_config: Dict, config: Dict) -> None:
        alignment_config = config.get("session_alignment", {})
        self.session_alignment_enabled = bool(alignment_config.get("enabled", False))
        self.reference_camera = alignment_config.get("reference_camera")
        self.world_to_reference = {}
        calibration_config = raw_config.get("session_alignment", {}).get("calibration", {})
        self.live_alignment_available = self.session_alignment_enabled and hasattr(cv2, "aruco")
        self.live_alignment_active = False
        self.live_alignment_image_stream = str(calibration_config.get("image_stream", "color") or "color")
        self.live_alignment_method = "board_relative"
        self.live_alignment_required_samples = int(calibration_config.get("required_samples", 12))
        self.live_alignment_window = max(
            int(calibration_config.get("stability_window", 20)),
            self.live_alignment_required_samples,
        )
        self.live_alignment_min_detected_tags = max(2, int(calibration_config.get("min_detected_tags", 4)))
        self.live_alignment_max_group_span_ns = int(float(calibration_config.get("max_group_span_ms", 180.0)) * 1_000_000)
        self.live_alignment_pending_image_limit = max(2, int(calibration_config.get("pending_image_limit", 8)))
        self.live_alignment_pending_max_age_ns = int(float(calibration_config.get("pending_max_age_ms", 500.0)) * 1_000_000)
        self.live_alignment_detection_buffer_limit = max(
            3,
            int(calibration_config.get("detection_buffer_limit", 20)),
        )
        self.live_alignment_detection_max_age_ns = int(
            float(calibration_config.get("detection_max_age_ms", 500.0)) * 1_000_000
        )
        self.live_alignment_image_scale = max(
            0.1,
            min(1.0, float(calibration_config.get("alignment_image_scale", 1.0))),
        )
        self.live_alignment_processing_hz = max(
            0.5,
            min(30.0, float(calibration_config.get("processing_hz", 10.0))),
        )
        self.live_alignment_display_axis_alignment = bool(
            calibration_config.get("display_axis_alignment", True)
        )
        self.live_alignment_dashboard_pose_max_age_ns = int(
            float(calibration_config.get("dashboard_pose_max_age_ms", 150.0)) * 1_000_000
        )
        self.live_alignment_dashboard_horizontal_yaw_mode = str(
            calibration_config.get("dashboard_horizontal_yaw_mode", "manual") or "manual"
        )
        self.live_alignment_dashboard_horizontal_yaw_deg = float(
            calibration_config.get("dashboard_horizontal_yaw_deg", 0.0)
        )
        self.live_alignment_lock_on_first_solution = bool(
            calibration_config.get("lock_on_first_solution", True)
        )
        self.live_alignment_reset_traces_on_lock = bool(
            calibration_config.get("reset_traces_on_lock", True)
        )
        self.live_alignment_anchor_rotation_mode = str(
            calibration_config.get("anchor_rotation_mode", "yaw") or "yaw"
        ).lower()
        if self.live_alignment_anchor_rotation_mode not in {"none", "yaw", "full"}:
            self.live_alignment_anchor_rotation_mode = "yaw"
        self.live_alignment_max_translation_err_m = float(calibration_config.get("max_translation_std_m", 0.25))
        self.live_alignment_max_rotation_err_deg = float(calibration_config.get("max_rotation_std_deg", 6.0))
        self.live_alignment_last_status = "live alignment idle"
        self.live_alignment_last_signature: Optional[Tuple[int, ...]] = None
        self.live_alignment_visible_cameras: int = 0
        self.live_alignment_inlier_counts: Dict[str, int] = {}
        self.live_alignment_logged_status: Optional[str] = None
        self.live_alignment_last_sync_span_ms: float = 0.0
        self.live_alignment_last_transform_summary: Dict[str, str] = {}
        self.live_alignment_last_raw_transform_summary: Dict[str, str] = {}
        self.live_alignment_last_anchor_summary: Dict[str, str] = {}
        self.live_alignment_last_tag_count: Dict[str, int] = {
            camera["name"]: 0
            for camera in raw_config.get("cameras", [])
            if camera.get("enabled", True)
        }
        self.live_alignment_last_summary_time: float = 0.0
        self.live_alignment_result_txt_path = Path(
            os.environ.get("INSIGHT_ALIGNMENT_RESULT", "/tmp/insight_live_alignment_result.txt")
        )
        default_state_path = Path(__file__).resolve().parents[1] / "config" / "alignment" / "live_alignment_state.json"
        self.live_alignment_state_path = Path(
            os.environ.get("INSIGHT_ALIGNMENT_STATE", str(default_state_path))
        )
        self.live_alignment_debug_state: Dict[str, Dict[str, object]] = {}

        dictionary_name = str(calibration_config.get("dictionary", "DICT_APRILTAG_36h11"))
        dictionary_id = getattr(cv2.aruco, dictionary_name)
        self.live_alignment_dictionary_name = dictionary_name
        self.live_alignment_aruco_dict = cv2.aruco.getPredefinedDictionary(dictionary_id)
        self.live_alignment_detector = None
        if hasattr(cv2.aruco, "ArucoDetector"):
            self.live_alignment_detector = cv2.aruco.ArucoDetector(
                self.live_alignment_aruco_dict,
                cv2.aruco.DetectorParameters(),
            )

        board_rows = int(calibration_config.get("board_rows", 6))
        board_cols = int(calibration_config.get("board_cols", 6))
        marker_length_m = float(calibration_config.get("marker_length_m", 0.055))
        marker_separation_m = float(calibration_config.get("marker_separation_m", 0.0165))
        if hasattr(cv2.aruco, "GridBoard"):
            self.live_alignment_board = cv2.aruco.GridBoard(
                (board_cols, board_rows),
                marker_length_m,
                marker_separation_m,
                self.live_alignment_aruco_dict,
            )
        else:
            self.live_alignment_board = cv2.aruco.GridBoard_create(
                board_cols,
                board_rows,
                marker_length_m,
                marker_separation_m,
                self.live_alignment_aruco_dict,
            )

    def _initialize_live_alignment_state(self) -> None:
        self.live_alignment_camera_matrix: Dict[str, Optional[np.ndarray]] = {
            camera.name: None for camera in self.cameras
        }
        self.live_alignment_dist_coeffs: Dict[str, Optional[np.ndarray]] = {
            camera.name: None for camera in self.cameras
        }
        self.live_alignment_latest_image: Dict[str, Optional[np.ndarray]] = {
            camera.name: None for camera in self.cameras
        }
        self.live_alignment_latest_image_stamp_ns: Dict[str, int] = {
            camera.name: -1 for camera in self.cameras
        }
        self.live_alignment_processed_stamp_ns: Dict[str, int] = {
            camera.name: -1 for camera in self.cameras
        }
        self.live_alignment_pending_images: Dict[str, List[Tuple[int, int, np.ndarray]]] = {
            camera.name: [] for camera in self.cameras
        }
        self.live_alignment_latest_detection: Dict[str, Optional[DetectionSample]] = {
            camera.name: None for camera in self.cameras
        }
        self.live_alignment_detection_buffer: Dict[str, List[DetectionSample]] = {
            camera.name: [] for camera in self.cameras
        }
        self.live_alignment_samples_by_camera: Dict[str, List[np.ndarray]] = {
            camera.name: [] for camera in self.cameras
        }
        self.live_alignment_topic_by_camera: Dict[str, str] = {}
        self._reset_live_alignment_debug_state()
        self._load_persisted_alignment_state()

    def _transform_pose_point(
        self, pose_name: str, point: Tuple[float, float, float]
    ) -> Tuple[float, float, float]:
        if not self.session_alignment_enabled:
            return point
        lock = getattr(self, "live_alignment_solution_lock", None)
        if lock is None:
            transform = self.world_to_reference.get(pose_name)
        else:
            with lock:
                transform = self.world_to_reference.get(pose_name)
        if transform is None:
            return point
        return transform_point(transform, point)

    def transformed_trace(self, pose_name: str) -> List[Tuple[float, float, float]]:
        raw_trace = self.raw_traces[pose_name]
        if not self.session_alignment_enabled:
            return list(raw_trace)
        lock = getattr(self, "live_alignment_solution_lock", None)
        if lock is None:
            transform = self.world_to_reference.get(pose_name)
        else:
            with lock:
                transform = self.world_to_reference.get(pose_name)
        if transform is None:
            return list(raw_trace)
        return [transform_point(transform, point) for point in raw_trace]

    def transformed_pose_transform(self, pose_name: str) -> Optional[np.ndarray]:
        latest_pose_sample = getattr(self, "latest_pose_sample", {}).get(pose_name)
        if latest_pose_sample is None:
            return None
        if not self.session_alignment_enabled:
            return latest_pose_sample.as_transform()
        lock = getattr(self, "live_alignment_solution_lock", None)
        if lock is None:
            transform = self.world_to_reference.get(pose_name)
        else:
            with lock:
                transform = self.world_to_reference.get(pose_name)
        if transform is None:
            return latest_pose_sample.as_transform()
        return transform @ latest_pose_sample.as_transform()

    def transformed_pose_sample(self, pose_name: str):
        latest_pose_sample = getattr(self, "latest_pose_sample", {}).get(pose_name)
        if latest_pose_sample is None:
            return None
        if not self.session_alignment_enabled:
            return latest_pose_sample
        lock = getattr(self, "live_alignment_solution_lock", None)
        if lock is None:
            transform = self.world_to_reference.get(pose_name)
        else:
            with lock:
                transform = self.world_to_reference.get(pose_name)
        if transform is None:
            return latest_pose_sample
        return transform_pose_sample(transform, latest_pose_sample)

    def start_live_alignment(self) -> str:
        if not self.live_alignment_available:
            self.live_alignment_last_status = "live alignment unavailable: OpenCV aruco or session alignment config missing"
            return self.live_alignment_last_status
        self.live_alignment_active = True
        self.world_to_reference = {}
        self.live_alignment_last_status = "alignment on"
        self.live_alignment_last_signature = None
        self.live_alignment_latest_detection = {camera.name: None for camera in self.cameras}
        self.live_alignment_detection_buffer = {camera.name: [] for camera in self.cameras}
        self.live_alignment_pending_images = {camera.name: [] for camera in self.cameras}
        self.live_alignment_processed_stamp_ns = {camera.name: -1 for camera in self.cameras}
        self.live_alignment_samples_by_camera = {camera.name: [] for camera in self.cameras}
        self.live_alignment_visible_cameras = 0
        self.live_alignment_inlier_counts = {}
        self.live_alignment_logged_status = None
        self.live_alignment_last_sync_span_ms = 0.0
        self.live_alignment_last_transform_summary = {}
        self.live_alignment_last_raw_transform_summary = {}
        self.live_alignment_last_anchor_summary = {}
        self.live_alignment_last_tag_count = {camera.name: 0 for camera in self.cameras}
        self.live_alignment_last_summary_time = 0.0
        self._reset_live_alignment_debug_state()
        self._reset_alignment_result_txt()
        self._log_live_alignment_status(force=True)
        self._emit_alignment_log(
            "config "
            f"method={self.live_alignment_method} stream={self.live_alignment_image_stream} "
            f"dict={self.live_alignment_dictionary_name} scale={self.live_alignment_image_scale:.2f} "
            f"processing_hz={self.live_alignment_processing_hz:.1f} "
            f"min_tags={self.live_alignment_min_detected_tags} required={self.live_alignment_required_samples} "
            f"display_axis_alignment={'on' if self.live_alignment_display_axis_alignment else 'off'} "
            f"lock_on_first_solution={'on' if self.live_alignment_lock_on_first_solution else 'off'} "
            f"reset_traces_on_lock={'on' if self.live_alignment_reset_traces_on_lock else 'off'} "
            f"anchor_rotation_mode={self.live_alignment_anchor_rotation_mode} "
            f"horizontal_yaw_mode={self.live_alignment_dashboard_horizontal_yaw_mode} "
            f"horizontal_yaw_deg={self.live_alignment_dashboard_horizontal_yaw_deg:.1f}"
        )
        for camera in self.cameras:
            self._emit_alignment_log(
                f"subscribe {camera.name}: image={self.live_alignment_topic_by_camera.get(camera.name, '-')} "
                f"info={camera.camera_info_topic} pose=not_used"
            )
        return self.alignment_status_text()

    def stop_live_alignment(self) -> str:
        self.live_alignment_active = False
        self.live_alignment_last_status = "alignment paused"
        self._log_live_alignment_status(force=True)
        return self.alignment_status_text()

    def _lock_live_alignment_solution(self) -> None:
        self.live_alignment_active = False
        self.live_alignment_last_status = "locked"
        self.live_alignment_last_signature = None
        self.live_alignment_latest_detection = {camera.name: None for camera in self.cameras}
        self.live_alignment_detection_buffer = {camera.name: [] for camera in self.cameras}
        self.live_alignment_pending_images = {camera.name: [] for camera in self.cameras}
        self.live_alignment_processed_stamp_ns = {camera.name: -1 for camera in self.cameras}
        self.live_alignment_samples_by_camera = {camera.name: [] for camera in self.cameras}
        if self.live_alignment_reset_traces_on_lock:
            for pose in self.poses:
                raw_trace = self.raw_traces.get(pose.name, [])
                if not raw_trace:
                    continue
                self.raw_traces[pose.name] = [raw_trace[-1]]
                self.latest_pose[pose.name] = self._transform_pose_point(pose.name, raw_trace[-1])
        self._persist_alignment_state()
        self._log_live_alignment_status(force=True)

    def _process_live_alignment(self) -> None:
        if not self.live_alignment_active:
            return
        now_monotonic_ns = time.monotonic_ns()
        for camera in self.cameras:
            lock = getattr(self, "live_alignment_image_lock", None)
            if lock is None:
                pending = list(self.live_alignment_pending_images[camera.name])
            else:
                with lock:
                    pending = list(self.live_alignment_pending_images[camera.name])

            if not pending:
                self.live_alignment_last_tag_count[camera.name] = 0
                self.live_alignment_latest_detection[camera.name] = None
                stage = "no_image" if self.live_alignment_latest_image_stamp_ns[camera.name] <= 0 else "waiting_image"
                self._set_alignment_debug(camera.name, stage=stage, tags=0, pending=0)
                continue
            self._prune_pending_alignment_images(camera.name, now_monotonic_ns)
            lock = getattr(self, "live_alignment_image_lock", None)
            if lock is None:
                pending = list(self.live_alignment_pending_images[camera.name])
            else:
                with lock:
                    pending = list(self.live_alignment_pending_images[camera.name])
            self._set_alignment_debug(camera.name, pending=len(pending))
            for stamp_ns, received_monotonic_ns, image in pending:
                if stamp_ns <= self.live_alignment_processed_stamp_ns[camera.name]:
                    self._drop_pending_alignment_images(camera.name, stamp_ns)
                    continue
                self._set_alignment_debug(
                    camera.name,
                    latency_ms=f"{(now_monotonic_ns - received_monotonic_ns) / 1_000_000.0:.1f}",
                )
                processed = self._process_live_alignment_image(camera.name, stamp_ns, image)
                if processed:
                    self.live_alignment_processed_stamp_ns[camera.name] = stamp_ns
                    self._drop_pending_alignment_images(camera.name, stamp_ns)
                break
        self._log_live_alignment_summary()

    def _process_live_alignment_image(self, camera_name: str, stamp_ns: int, image_bgr: np.ndarray) -> bool:
        if self.live_alignment_detector is not None:
            corners, ids, _ = self.live_alignment_detector.detectMarkers(image_bgr)
        else:
            corners, ids, _ = cv2.aruco.detectMarkers(image_bgr, self.live_alignment_aruco_dict)
        if ids is None or len(ids) < self.live_alignment_min_detected_tags:
            self.live_alignment_last_tag_count[camera_name] = 0 if ids is None else int(len(ids))
            self.live_alignment_latest_detection[camera_name] = None
            self._set_alignment_debug(
                camera_name,
                stage="tags_low",
                tags=self.live_alignment_last_tag_count[camera_name],
            )
            return True
        self.live_alignment_last_tag_count[camera_name] = int(len(ids))
        self._set_alignment_debug(camera_name, stage="tags_ok", tags=int(len(ids)))

        camera_matrix = self.live_alignment_camera_matrix[camera_name]
        dist_coeffs = self.live_alignment_dist_coeffs[camera_name]
        if camera_matrix is None or dist_coeffs is None:
            self.live_alignment_latest_detection[camera_name] = None
            self._set_alignment_debug(camera_name, stage="missing_camera_info")
            return False
        detection_image = image_bgr
        detection_camera_matrix = camera_matrix
        if self.live_alignment_image_scale != 1.0:
            height, width = image_bgr.shape[:2]
            scaled_width = max(1, int(round(width * self.live_alignment_image_scale)))
            scaled_height = max(1, int(round(height * self.live_alignment_image_scale)))
            detection_image = cv2.resize(
                image_bgr,
                (scaled_width, scaled_height),
                interpolation=cv2.INTER_AREA,
            )
            detection_camera_matrix = camera_matrix.copy()
            detection_camera_matrix[0, 0] *= self.live_alignment_image_scale
            detection_camera_matrix[1, 1] *= self.live_alignment_image_scale
            detection_camera_matrix[0, 2] *= self.live_alignment_image_scale
            detection_camera_matrix[1, 2] *= self.live_alignment_image_scale
        if detection_image is not image_bgr:
            if self.live_alignment_detector is not None:
                corners, ids, _ = self.live_alignment_detector.detectMarkers(detection_image)
            else:
                corners, ids, _ = cv2.aruco.detectMarkers(detection_image, self.live_alignment_aruco_dict)
            if ids is None or len(ids) < self.live_alignment_min_detected_tags:
                self.live_alignment_last_tag_count[camera_name] = 0 if ids is None else int(len(ids))
                self.live_alignment_latest_detection[camera_name] = None
                self._set_alignment_debug(
                    camera_name,
                    stage="tags_low",
                    tags=self.live_alignment_last_tag_count[camera_name],
                )
                return True
        try:
            estimate = cv2.aruco.estimatePoseBoard(
                corners,
                ids,
                self.live_alignment_board,
                detection_camera_matrix,
                dist_coeffs,
                None,
                None,
            )
        except TypeError:
            estimate = cv2.aruco.estimatePoseBoard(
                corners,
                ids,
                self.live_alignment_board,
                detection_camera_matrix,
                dist_coeffs,
            )
        except cv2.error:
            estimate = cv2.aruco.estimatePoseBoard(
                corners,
                ids,
                self.live_alignment_board,
                detection_camera_matrix,
                dist_coeffs,
                None,
                None,
            )
        retval = None
        if isinstance(estimate, tuple):
            if len(estimate) == 3:
                retval, rvec, tvec = estimate
            else:
                _, rvec, tvec = estimate
                retval = 0 if rvec is None or tvec is None else len(ids)
        else:
            retval = int(estimate)
            rvec = None
            tvec = None
        num_markers = int(retval or 0)
        if retval is None or float(retval) <= 0.0 or rvec is None or tvec is None:
            self.live_alignment_latest_detection[camera_name] = None
            self._set_alignment_debug(camera_name, stage="pose_board_failed")
            return True
        rotation, _ = cv2.Rodrigues(rvec)
        t_camera_board = matrix_to_transform(rotation, tvec.reshape(3))
        self.live_alignment_latest_detection[camera_name] = DetectionSample(
            stamp_ns=stamp_ns,
            marker_transform=t_camera_board,
        )
        self._store_live_alignment_detection(camera_name, self.live_alignment_latest_detection[camera_name])
        self._set_alignment_debug(
            camera_name,
            stage="detection_ok",
            tags=self.live_alignment_last_tag_count[camera_name],
        )
        self._update_live_alignment_solution()
        return True

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
        rotation_map = self._optical_to_dashboard_rotation()
        translation = rotation_map @ optical_transform[:3, 3]
        return matrix_to_transform(np.eye(3, dtype=np.float64), translation)

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
        if self.live_alignment_dashboard_horizontal_yaw_mode == "manual":
            return self.live_alignment_dashboard_horizontal_yaw_deg
        if self.live_alignment_dashboard_horizontal_yaw_mode != "auto_center_non_reference":
            return 0.0
        horizontal_vectors = []
        for camera in self.cameras:
            if camera.name == self.reference_camera:
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
        rotation = self._rotation_about_display_z(yaw_deg)
        rotated = {}
        for camera_name, transform in transforms.items():
            translation = rotation @ transform[:3, 3]
            rotated[camera_name] = matrix_to_transform(transform[:3, :3], translation)
        return rotated

    def _canonicalize_display_transforms(
        self,
        transforms: Dict[str, np.ndarray],
    ) -> Dict[str, np.ndarray]:
        if not self.live_alignment_display_axis_alignment:
            return transforms
        canonical = {}
        identity = np.eye(3, dtype=np.float64)
        for camera_name, transform in transforms.items():
            canonical[camera_name] = matrix_to_transform(identity, transform[:3, 3])
        return canonical

    def _find_dashboard_pose_sample(self, camera_name: str, stamp_ns: int):
        with self.pose_history_lock:
            history = list(self.pose_history.get(camera_name, []))
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
            if span_ns <= max(self.live_alignment_dashboard_pose_max_age_ns, 1):
                return interpolate_pose_sample(before, after, stamp_ns)
        best_sample = min(history, key=lambda sample: abs(sample.stamp_ns - stamp_ns))
        if abs(best_sample.stamp_ns - stamp_ns) > self.live_alignment_dashboard_pose_max_age_ns:
            return None
        return best_sample

    def _build_dashboard_world_anchors(
        self,
        detections: Dict[str, DetectionSample],
        display_camera_transforms: Dict[str, np.ndarray],
    ) -> Optional[Dict[str, np.ndarray]]:
        anchors: Dict[str, np.ndarray] = {}
        for camera in self.cameras:
            pose_sample = self._find_dashboard_pose_sample(camera.name, detections[camera.name].stamp_ns)
            if pose_sample is None:
                self._emit_alignment_log(
                    f"dashboard anchor missing pose for {camera.name} at stamp={detections[camera.name].stamp_ns}"
                )
                return None
            pose_transform = pose_sample.as_transform()
            if self.live_alignment_anchor_rotation_mode == "none":
                display_translation = display_camera_transforms[camera.name][:3, 3]
                pose_translation = np.array(pose_sample.position, dtype=np.float64)
                anchor_translation = display_translation - pose_translation
                anchors[camera.name] = matrix_to_transform(np.eye(3, dtype=np.float64), anchor_translation)
            elif self.live_alignment_anchor_rotation_mode == "yaw":
                display_transform = display_camera_transforms[camera.name]
                target_yaw = math.atan2(
                    float(display_transform[1, 0]),
                    float(display_transform[0, 0]),
                )
                pose_yaw = math.atan2(
                    float(pose_transform[1, 0]),
                    float(pose_transform[0, 0]),
                )
                anchor_rotation = self._rotation_about_display_z(math.degrees(target_yaw - pose_yaw))
                display_translation = display_transform[:3, 3]
                pose_translation = pose_transform[:3, 3]
                anchor_translation = display_translation - anchor_rotation @ pose_translation
                anchors[camera.name] = matrix_to_transform(anchor_rotation, anchor_translation)
            else:
                anchors[camera.name] = display_camera_transforms[camera.name] @ invert_transform(pose_transform)
        return anchors

    def _store_live_alignment_detection(self, camera_name: str, sample: DetectionSample) -> None:
        lock = getattr(self, "live_alignment_solution_lock", None)
        if lock is None:
            self._store_live_alignment_detection_unlocked(camera_name, sample)
            return
        with lock:
            self._store_live_alignment_detection_unlocked(camera_name, sample)

    def _store_live_alignment_detection_unlocked(self, camera_name: str, sample: DetectionSample) -> None:
        self.live_alignment_latest_detection[camera_name] = sample
        buffer = self.live_alignment_detection_buffer[camera_name]
        buffer.append(sample)
        newest_stamp_ns = sample.stamp_ns
        min_stamp_ns = newest_stamp_ns - self.live_alignment_detection_max_age_ns
        buffer[:] = [item for item in buffer if item.stamp_ns >= min_stamp_ns]
        if len(buffer) > self.live_alignment_detection_buffer_limit:
            del buffer[: len(buffer) - self.live_alignment_detection_buffer_limit]

    def _drop_pending_alignment_images(self, camera_name: str, through_stamp_ns: int) -> None:
        lock = getattr(self, "live_alignment_image_lock", None)
        if lock is None:
            self.live_alignment_pending_images[camera_name] = [
                item for item in self.live_alignment_pending_images[camera_name]
                if item[0] > through_stamp_ns
            ]
            return
        with lock:
            self.live_alignment_pending_images[camera_name] = [
                item for item in self.live_alignment_pending_images[camera_name]
                if item[0] > through_stamp_ns
            ]

    def _prune_pending_alignment_images(self, camera_name: str, now_monotonic_ns: int) -> None:
        min_received_ns = now_monotonic_ns - self.live_alignment_pending_max_age_ns
        lock = getattr(self, "live_alignment_image_lock", None)
        if lock is None:
            self.live_alignment_pending_images[camera_name] = [
                item for item in self.live_alignment_pending_images[camera_name]
                if item[1] >= min_received_ns
            ]
            if len(self.live_alignment_pending_images[camera_name]) > self.live_alignment_pending_image_limit:
                del self.live_alignment_pending_images[camera_name][
                    : len(self.live_alignment_pending_images[camera_name]) - self.live_alignment_pending_image_limit
                ]
            return
        with lock:
            self.live_alignment_pending_images[camera_name] = [
                item for item in self.live_alignment_pending_images[camera_name]
                if item[1] >= min_received_ns
            ]
            if len(self.live_alignment_pending_images[camera_name]) > self.live_alignment_pending_image_limit:
                del self.live_alignment_pending_images[camera_name][
                    : len(self.live_alignment_pending_images[camera_name]) - self.live_alignment_pending_image_limit
                ]

    def _update_live_alignment_solution(self) -> None:
        lock = getattr(self, "live_alignment_solution_lock", None)
        if lock is None:
            detections = self._find_live_alignment_detection_group_unlocked()
        else:
            with lock:
                detections = self._find_live_alignment_detection_group_unlocked()
        if detections is None:
            self._log_live_alignment_status()
            return
        self.live_alignment_visible_cameras = len(detections)
        stamps = [sample.stamp_ns for sample in detections.values()]
        self.live_alignment_last_sync_span_ms = (max(stamps) - min(stamps)) / 1_000_000.0
        if max(stamps) - min(stamps) > self.live_alignment_max_group_span_ns:
            self.live_alignment_last_status = "waiting-sync"
            self._log_live_alignment_status()
            return
        signature = tuple(stamps)
        if signature == self.live_alignment_last_signature:
            return
        self.live_alignment_last_signature = signature
        reference_detection = detections[self.reference_camera]
        raw_transforms = {self.reference_camera: np.eye(4, dtype=np.float64)}
        for camera in self.cameras:
            if camera.name == self.reference_camera:
                continue
            current = detections[camera.name]
            transform = reference_detection.marker_transform @ invert_transform(current.marker_transform)
            samples = self.live_alignment_samples_by_camera[camera.name]
            samples.append(transform)
            if len(samples) > self.live_alignment_window:
                del samples[: len(samples) - self.live_alignment_window]
        counts = {}
        for camera in self.cameras:
            if camera.name == self.reference_camera:
                counts[camera.name] = self.live_alignment_required_samples
                continue
            inliers = self._inlier_transforms(self.live_alignment_samples_by_camera[camera.name])
            counts[camera.name] = len(inliers)
            if len(inliers) < self.live_alignment_required_samples:
                self.live_alignment_inlier_counts = counts
                self.live_alignment_last_status = "collecting"
                self._log_live_alignment_status()
                return
            averaged = average_transforms(inliers[-self.live_alignment_required_samples :])
            if averaged is None:
                return
            raw_transforms[camera.name] = averaged
        raw_transforms[self.reference_camera] = np.eye(4, dtype=np.float64)
        base_transforms = {
            camera.name: self._dashboard_transform_from_optical(raw_transforms[camera.name])
            for camera in self.cameras
        }
        dashboard_yaw_deg = self._dashboard_horizontal_yaw_deg_from_transforms(base_transforms)
        display_camera_transforms = self._apply_dashboard_horizontal_yaw(base_transforms, dashboard_yaw_deg)
        display_camera_transforms = self._canonicalize_display_transforms(display_camera_transforms)
        trajectory_anchor_transforms = self._build_dashboard_world_anchors(detections, display_camera_transforms)
        if trajectory_anchor_transforms is None:
            self.live_alignment_last_status = "waiting-sync"
            self._log_live_alignment_status()
            return
        if lock is None:
            self.world_to_reference = trajectory_anchor_transforms
            self.live_alignment_last_transform_summary = {
                camera.name: self._format_transform_summary(display_camera_transforms[camera.name])
                for camera in self.cameras
            }
            self.live_alignment_last_raw_transform_summary = {
                camera.name: self._format_transform_summary(raw_transforms[camera.name])
                for camera in self.cameras
            }
            self.live_alignment_last_anchor_summary = {
                camera.name: self._format_transform_summary(trajectory_anchor_transforms[camera.name])
                for camera in self.cameras
            }
        else:
            with lock:
                self.world_to_reference = trajectory_anchor_transforms
                self.live_alignment_last_transform_summary = {
                    camera.name: self._format_transform_summary(display_camera_transforms[camera.name])
                    for camera in self.cameras
                }
                self.live_alignment_last_raw_transform_summary = {
                    camera.name: self._format_transform_summary(raw_transforms[camera.name])
                    for camera in self.cameras
                }
                self.live_alignment_last_anchor_summary = {
                    camera.name: self._format_transform_summary(trajectory_anchor_transforms[camera.name])
                    for camera in self.cameras
                }
        self._write_alignment_result_txt(detections, raw_transforms, display_camera_transforms, trajectory_anchor_transforms)
        self._refresh_transformed_poses()
        self.live_alignment_inlier_counts = counts
        self.live_alignment_last_status = "tracking"
        self._log_live_alignment_status()
        self._emit_alignment_log(f"reference_camera={self.reference_camera}")
        self._emit_alignment_log("axis_convention: reference optical frame (x right, y down, z forward)")
        self._emit_alignment_log("dashboard_frame: x=forward, y=right, z=up")
        self._emit_alignment_log(
            f"display_axes_canonical={'on' if self.live_alignment_display_axis_alignment else 'off'}"
        )
        self._emit_alignment_log(f"dashboard_horizontal_yaw_deg={dashboard_yaw_deg:.1f}")
        self._emit_alignment_log("final camera transforms relative to reference:")
        for camera in self.cameras:
            raw_transform = raw_transforms.get(camera.name)
            display_transform = display_camera_transforms.get(camera.name)
            anchor_transform = trajectory_anchor_transforms.get(camera.name)
            if raw_transform is None or display_transform is None or anchor_transform is None:
                continue
            raw_translation = raw_transform[:3, 3]
            display_translation = display_transform[:3, 3]
            anchor_translation = anchor_transform[:3, 3]
            self._emit_alignment_log(
                f"{camera.name}: optical=({raw_translation[0]:.3f}, {raw_translation[1]:.3f}, {raw_translation[2]:.3f}) "
                f"display=({display_translation[0]:.3f}, {display_translation[1]:.3f}, {display_translation[2]:.3f}) "
                f"anchor=({anchor_translation[0]:.3f}, {anchor_translation[1]:.3f}, {anchor_translation[2]:.3f}) "
                f"height_up={display_translation[2]:.3f}"
            )
        if self.live_alignment_lock_on_first_solution:
            self._emit_alignment_log("solution locked: future VIO uses this calibrated relative offset")
            self._lock_live_alignment_solution()
        else:
            self._persist_alignment_state()

    def _find_live_alignment_detection_group_unlocked(self) -> Optional[Dict[str, DetectionSample]]:
        newest_stamp_ns = max(
            (
                buffer[-1].stamp_ns
                for buffer in self.live_alignment_detection_buffer.values()
                if buffer
            ),
            default=0,
        )
        if newest_stamp_ns > 0:
            min_stamp_ns = newest_stamp_ns - self.live_alignment_detection_max_age_ns
            for camera in self.cameras:
                buffer = self.live_alignment_detection_buffer.get(camera.name, [])
                if not buffer:
                    continue
                buffer[:] = [sample for sample in buffer if sample.stamp_ns >= min_stamp_ns]
        buffers = {
            camera.name: list(self.live_alignment_detection_buffer.get(camera.name, []))
            for camera in self.cameras
        }
        available = {name: buffer for name, buffer in buffers.items() if buffer}
        self.live_alignment_visible_cameras = len(available)
        if self.reference_camera not in available:
            self.live_alignment_last_status = "waiting-reference"
            return None
        if len(available) < len(self.cameras):
            self.live_alignment_last_status = "waiting-board"
            return None

        best_group = None
        best_span = None
        for reference_sample in available[self.reference_camera]:
            group = {self.reference_camera: reference_sample}
            for camera in self.cameras:
                if camera.name == self.reference_camera:
                    continue
                candidate = min(
                    available[camera.name],
                    key=lambda sample: abs(sample.stamp_ns - reference_sample.stamp_ns),
                )
                group[camera.name] = candidate
            stamps = [sample.stamp_ns for sample in group.values()]
            span_ns = max(stamps) - min(stamps)
            if best_span is None or span_ns < best_span:
                best_span = span_ns
                best_group = group

        self.live_alignment_last_sync_span_ms = 0.0 if best_span is None else best_span / 1_000_000.0
        if best_group is None:
            self.live_alignment_last_status = "waiting-board"
            return None
        if best_span is not None and best_span > self.live_alignment_max_group_span_ns:
            self.live_alignment_last_status = "waiting-sync"
            return None
        return best_group

    def _refresh_transformed_poses(self) -> None:
        for pose in self.poses:
            raw_trace = self.raw_traces[pose.name]
            if raw_trace:
                self.latest_pose[pose.name] = self._transform_pose_point(pose.name, raw_trace[-1])

    def _inlier_transforms(self, transforms: List[np.ndarray]) -> List[np.ndarray]:
        if len(transforms) < 3:
            return list(transforms)
        consensus_center = average_transforms(transforms)
        if consensus_center is None:
            return []
        errors = [self._transform_delta_metrics(consensus_center, transform) for transform in transforms]
        translation_errors = np.array([item[0] for item in errors], dtype=np.float64)
        rotation_errors = np.array([item[1] for item in errors], dtype=np.float64)
        translation_median = float(np.median(translation_errors))
        rotation_median = float(np.median(rotation_errors))
        translation_mad = float(np.median(np.abs(translation_errors - translation_median)))
        rotation_mad = float(np.median(np.abs(rotation_errors - rotation_median)))
        translation_gate = max(self.live_alignment_max_translation_err_m, translation_median + 3.0 * max(translation_mad, 1e-4))
        rotation_gate = max(self.live_alignment_max_rotation_err_deg, rotation_median + 3.0 * max(rotation_mad, 0.05))
        return [
            transform
            for transform, (translation_error, rotation_error) in zip(transforms, errors)
            if translation_error <= translation_gate and rotation_error <= rotation_gate
        ]

    def _transform_delta_metrics(self, reference: np.ndarray, candidate: np.ndarray) -> Tuple[float, float]:
        delta = reference @ invert_transform(candidate)
        translation_norm_m = float(np.linalg.norm(delta[:3, 3]))
        trace = float(np.trace(delta[:3, :3]))
        cos_theta = max(-1.0, min(1.0, (trace - 1.0) * 0.5))
        rotation_angle_deg = math.degrees(math.acos(cos_theta))
        return translation_norm_m, rotation_angle_deg

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
        status = self.alignment_status_text()
        if not force and status == self.live_alignment_logged_status:
            return
        self.live_alignment_logged_status = status
        self._emit_alignment_log(status)

    def _log_live_alignment_summary(self) -> None:
        now = time.monotonic()
        if self.live_alignment_last_summary_time > 0.0 and (now - self.live_alignment_last_summary_time) < 1.0:
            return
        self.live_alignment_last_summary_time = now
        parts = []
        seen = []
        usable = []
        missing_images = []
        detection_ok = []
        pnp_failed = []
        pending_parts = []
        for camera in self.cameras:
            has_image = self.live_alignment_latest_image_stamp_ns[camera.name] > 0
            tag_count = self.live_alignment_last_tag_count.get(camera.name, 0)
            state = self.live_alignment_debug_state.get(camera.name, {})
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
            if tag_count >= self.live_alignment_min_detected_tags:
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
        self._emit_alignment_log(
            f"tags {' '.join(parts)} | seen={seen_text} | usable={usable_text}{missing_text}{stage_text} | {self.alignment_status_text()}"
        )

    def _emit_alignment_log(self, message: str) -> None:
        text = f"[alignment] {message}"
        print(text, flush=True)

    def _reset_live_alignment_debug_state(self) -> None:
        self.live_alignment_debug_state = {
            camera.name: {
                "stage": "idle",
                "tags": 0,
                "pending": 0,
                "latency_ms": "-",
            }
            for camera in self.cameras
        }

    def _set_alignment_debug(self, camera_name: str, **updates: object) -> None:
        state = self.live_alignment_debug_state.setdefault(camera_name, {})
        state.update(updates)

    def _reset_alignment_result_txt(self) -> None:
        try:
            self.live_alignment_result_txt_path.parent.mkdir(parents=True, exist_ok=True)
            header = (
                "# Insight live alignment latest result\n"
                f"# file={self.live_alignment_result_txt_path}\n"
                f"# reference_camera={self.reference_camera}\n"
                f"# method={self.live_alignment_method}\n"
            )
            self.live_alignment_result_txt_path.write_text(header, encoding="utf-8")
            self._emit_alignment_log(f"result txt: {self.live_alignment_result_txt_path}")
        except Exception as exc:
            self._emit_alignment_log(f"result txt unavailable: {exc}")

    def _write_alignment_result_txt(
        self,
        detections: Dict[str, DetectionSample],
        raw_transforms: Dict[str, np.ndarray],
        display_camera_transforms: Dict[str, np.ndarray],
        trajectory_anchor_transforms: Dict[str, np.ndarray],
    ) -> None:
        try:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            lines = [
                "# Insight live alignment latest result",
                f"time={timestamp}",
                f"reference_camera={self.reference_camera}",
                f"status={self.alignment_status_text()}",
                f"sync_span_ms={self.live_alignment_last_sync_span_ms:.1f}",
                "",
            ]
            for camera in self.cameras:
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
                lines.append("")
            self.live_alignment_result_txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception as exc:
            self._emit_alignment_log(f"result txt write failed: {exc}")

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
        if not self.session_alignment_enabled or not self.world_to_reference:
            return
        try:
            transforms = {}
            lock = getattr(self, "live_alignment_solution_lock", None)
            if lock is None:
                source_transforms = dict(self.world_to_reference)
            else:
                with lock:
                    source_transforms = dict(self.world_to_reference)
            for camera_name, transform in source_transforms.items():
                transforms[camera_name] = self._serialize_transform(transform)
            payload = {
                "version": 1,
                "saved_at_epoch_s": time.time(),
                "saved_at_local": time.strftime("%Y-%m-%d %H:%M:%S"),
                "reference_camera": self.reference_camera,
                "status": self.alignment_status_text(),
                "display_axis_alignment": bool(self.live_alignment_display_axis_alignment),
                "anchor_rotation_mode": self.live_alignment_anchor_rotation_mode,
                "dashboard_horizontal_yaw_mode": self.live_alignment_dashboard_horizontal_yaw_mode,
                "dashboard_horizontal_yaw_deg": float(self.live_alignment_dashboard_horizontal_yaw_deg),
                "camera_names": [camera.name for camera in self.cameras],
                "world_to_reference": transforms,
            }
            self.live_alignment_state_path.parent.mkdir(parents=True, exist_ok=True)
            self.live_alignment_state_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
                encoding="utf-8",
            )
            self._emit_alignment_log(f"alignment state saved: {self.live_alignment_state_path}")
        except Exception as exc:
            self._emit_alignment_log(f"alignment state save failed: {exc}")

    def _load_persisted_alignment_state(self) -> None:
        if not self.session_alignment_enabled:
            return
        if not self.live_alignment_state_path.exists():
            return
        try:
            payload = json.loads(self.live_alignment_state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self._emit_alignment_log(f"alignment state load failed: {exc}")
            return
        transforms_payload = payload.get("world_to_reference")
        if not isinstance(transforms_payload, dict):
            return
        available_cameras = {camera.name for camera in self.cameras}
        loaded_transforms: Dict[str, np.ndarray] = {}
        for camera_name, transform_value in transforms_payload.items():
            if camera_name not in available_cameras:
                continue
            matrix = self._deserialize_transform(transform_value)
            if matrix is None:
                continue
            loaded_transforms[camera_name] = matrix
        if not loaded_transforms:
            return
        if self.reference_camera in available_cameras:
            persisted_reference = str(payload.get("reference_camera") or self.reference_camera)
            if persisted_reference == self.reference_camera:
                identity = np.eye(4, dtype=np.float64)
                loaded_transforms.setdefault(self.reference_camera, identity)
        lock = getattr(self, "live_alignment_solution_lock", None)
        if lock is None:
            self.world_to_reference = loaded_transforms
        else:
            with lock:
                self.world_to_reference = loaded_transforms
        self.live_alignment_last_status = "locked"
        self.live_alignment_last_transform_summary = {
            camera_name: self._format_transform_summary(transform)
            for camera_name, transform in loaded_transforms.items()
        }
        self._refresh_transformed_poses()
        self._emit_alignment_log(f"alignment state loaded: {self.live_alignment_state_path}")

    def _decode_calibration_message(self, topic_type: str, msg: object) -> Optional[np.ndarray]:
        if topic_type == "compressed":
            return cv2.imdecode(np.frombuffer(msg.data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if not isinstance(msg, RosImage) or msg.width == 0 or msg.height == 0:
            return None
        data = np.frombuffer(msg.data, dtype=np.uint8)
        encoding = msg.encoding.lower()
        if encoding == "bgr8":
            return np.ascontiguousarray(data.reshape((msg.height, msg.width, 3)))
        if encoding == "rgb8":
            rgb = data.reshape((msg.height, msg.width, 3))
            return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        if encoding in ("mono8", "8uc1"):
            gray = data.reshape((msg.height, msg.width))
            return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        return None

    def alignment_status_text(self) -> str:
        if not self.session_alignment_enabled:
            return "alignment disabled"
        if self.live_alignment_active:
            if self.live_alignment_last_status == "waiting-reference":
                return f"Alignment ON | waiting {self.reference_camera}"
            if self.live_alignment_last_status == "waiting-board":
                return f"Alignment ON | board {self.live_alignment_visible_cameras}/{len(self.cameras)}"
            if self.live_alignment_last_status == "waiting-sync":
                return "Alignment ON | sync"
            if self.live_alignment_last_status == "collecting":
                done = min(self.live_alignment_inlier_counts.values(), default=0)
                return f"Alignment ON | samples {done}/{self.live_alignment_required_samples}"
            if self.live_alignment_last_status == "tracking":
                return "Alignment ON | tracking"
            return "Alignment ON"
        if not self.world_to_reference:
            return "Alignment OFF"
        return "Alignment OFF | locked"
