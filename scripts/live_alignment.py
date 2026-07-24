"""Compatibility facade for the split online alignment services."""

from typing import Dict, List, Optional, Tuple

import numpy as np

from alignment import AlignmentConsensus, AlignmentController, AlignmentTransforms, DetectionSample


class LiveAlignmentMixin:
    def _alignment_controller_instance(self) -> AlignmentController:
        controller = getattr(self, "_alignment_controller", None)
        if controller is None:
            controller = AlignmentController(self)
            self._alignment_controller = controller
        return controller

    def _configure_live_alignment(self, raw_config: Dict, config: Dict) -> None:
        return self._alignment_controller_instance().config._configure_live_alignment(raw_config, config)

    def _initialize_live_alignment_state(self) -> None:
        return self._alignment_controller_instance().config._initialize_live_alignment_state()

    def _transform_pose_point(self, pose_name: str, point: Tuple[float, float, float]) -> Tuple[float, float, float]:
        return self._alignment_controller_instance().transforms._transform_pose_point(pose_name, point)

    def transformed_trace(self, pose_name: str) -> List[Tuple[float, float, float]]:
        return self._alignment_controller_instance().transforms.transformed_trace(pose_name)

    def transform_trace_points(
        self,
        pose_name: str,
        points: List[Tuple[float, float, float]],
    ) -> List[Tuple[float, float, float]]:
        return self._alignment_controller_instance().transforms.transform_trace_points(
            pose_name, points
        )

    def transformed_pose_sample(self, pose_name: str):
        return self._alignment_controller_instance().transforms.transformed_pose_sample(pose_name)

    def start_live_alignment(self) -> str:
        return self._alignment_controller_instance().lifecycle.start_live_alignment()

    def stop_live_alignment(self) -> str:
        return self._alignment_controller_instance().lifecycle.stop_live_alignment()

    def _lock_live_alignment_solution(self) -> None:
        return self._alignment_controller_instance().lifecycle._lock_live_alignment_solution()

    def _set_live_alignment_timer_enabled(self, enabled: bool) -> None:
        return self._alignment_controller_instance().lifecycle._set_live_alignment_timer_enabled(enabled)

    def _process_live_alignment(self) -> None:
        return self._alignment_controller_instance().lifecycle._process_live_alignment()

    def _process_live_alignment_image(self, camera_name: str, stamp_ns: int, image_bgr: np.ndarray) -> bool:
        return self._alignment_controller_instance().lifecycle._process_live_alignment_image(camera_name, stamp_ns, image_bgr)

    def _solve_board_pose(self, corners, ids, camera_matrix: np.ndarray, dist_coeffs: np.ndarray) -> Optional[Tuple[np.ndarray, np.ndarray, Optional[float]]]:
        return self._alignment_controller_instance().detector._solve_board_pose(corners, ids, camera_matrix, dist_coeffs)

    def _disambiguate_planar_pose(self, obj_in: np.ndarray, img_in: np.ndarray, camera_matrix: np.ndarray, dist_coeffs: np.ndarray) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        return self._alignment_controller_instance().detector._disambiguate_planar_pose(obj_in, img_in, camera_matrix, dist_coeffs)

    def _solve_board_pose_legacy(self, corners, ids, camera_matrix: np.ndarray, dist_coeffs: np.ndarray) -> Optional[Tuple[np.ndarray, np.ndarray, Optional[float]]]:
        return self._alignment_controller_instance().detector._solve_board_pose_legacy(corners, ids, camera_matrix, dist_coeffs)

    @staticmethod
    def _optical_to_dashboard_rotation() -> np.ndarray:
        return AlignmentTransforms._optical_to_dashboard_rotation()

    def _dashboard_transform_from_optical(self, optical_transform: np.ndarray) -> np.ndarray:
        return self._alignment_controller_instance().transforms._dashboard_transform_from_optical(optical_transform)

    @staticmethod
    def _rotation_about_display_z(yaw_deg: float) -> np.ndarray:
        return AlignmentTransforms._rotation_about_display_z(yaw_deg)

    def _dashboard_horizontal_yaw_deg_from_transforms(self, transforms: Dict[str, np.ndarray]) -> float:
        return self._alignment_controller_instance().transforms._dashboard_horizontal_yaw_deg_from_transforms(transforms)

    def _apply_dashboard_horizontal_yaw(self, transforms: Dict[str, np.ndarray], yaw_deg: float) -> Dict[str, np.ndarray]:
        return self._alignment_controller_instance().transforms._apply_dashboard_horizontal_yaw(transforms, yaw_deg)

    def _canonicalize_display_transforms(self, transforms: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
        return self._alignment_controller_instance().transforms._canonicalize_display_transforms(transforms)

    def _find_dashboard_pose_sample(self, camera_name: str, stamp_ns: int):
        return self._alignment_controller_instance().transforms._find_dashboard_pose_sample(camera_name, stamp_ns)

    def _build_dashboard_world_anchor(self, camera_name: str, stamp_ns: int, display_transform: np.ndarray) -> Optional[np.ndarray]:
        return self._alignment_controller_instance().transforms._build_dashboard_world_anchor(camera_name, stamp_ns, display_transform)

    def _store_live_alignment_detection(self, camera_name: str, sample: DetectionSample) -> None:
        return self._alignment_controller_instance().lifecycle._store_live_alignment_detection(camera_name, sample)

    def _store_live_alignment_detection_unlocked(self, camera_name: str, sample: DetectionSample) -> None:
        return self._alignment_controller_instance().lifecycle._store_live_alignment_detection_unlocked(camera_name, sample)

    def _drop_pending_alignment_images(self, camera_name: str, through_stamp_ns: int) -> None:
        return self._alignment_controller_instance().lifecycle._drop_pending_alignment_images(camera_name, through_stamp_ns)

    def _prune_pending_alignment_images(self, camera_name: str, now_monotonic_ns: int) -> None:
        return self._alignment_controller_instance().lifecycle._prune_pending_alignment_images(camera_name, now_monotonic_ns)

    def _update_live_alignment_solution(self, camera_name: str) -> None:
        return self._alignment_controller_instance().lifecycle._update_live_alignment_solution(camera_name)

    def _refresh_transformed_poses(self) -> None:
        return self._alignment_controller_instance().lifecycle._refresh_transformed_poses()

    def _inlier_transform_indices(self, transforms: List[np.ndarray], translation_floor_m: float, rotation_floor_deg: float) -> List[int]:
        return self._alignment_controller_instance().consensus._inlier_transform_indices(transforms, translation_floor_m, rotation_floor_deg)

    @staticmethod
    def _pose_delta_metrics(reference: np.ndarray, candidate: np.ndarray) -> Tuple[float, float]:
        return AlignmentConsensus._pose_delta_metrics(reference, candidate)

    def _format_transform_summary(self, transform: np.ndarray) -> str:
        return self._alignment_controller_instance().diagnostics._format_transform_summary(transform)

    def _log_live_alignment_status(self, force: bool = False) -> None:
        return self._alignment_controller_instance().diagnostics._log_live_alignment_status(force)

    def _log_live_alignment_summary(self) -> None:
        return self._alignment_controller_instance().diagnostics._log_live_alignment_summary()

    def _emit_alignment_log(self, message: str) -> None:
        return self._alignment_controller_instance().diagnostics._emit_alignment_log(message)

    def _reset_live_alignment_debug_state(self) -> None:
        return self._alignment_controller_instance().diagnostics._reset_live_alignment_debug_state()

    def _set_alignment_debug(self, camera_name: str, **updates: object) -> None:
        return self._alignment_controller_instance().diagnostics._set_alignment_debug(camera_name, **updates)

    def _reset_alignment_result_txt(self) -> None:
        return self._alignment_controller_instance().diagnostics._reset_alignment_result_txt()

    def _write_alignment_result_txt(self, detections: Dict[str, DetectionSample], raw_transforms: Dict[str, np.ndarray], display_camera_transforms: Dict[str, np.ndarray], trajectory_anchor_transforms: Dict[str, np.ndarray], anchor_quality: Optional[Dict[str, str]] = None) -> None:
        return self._alignment_controller_instance().diagnostics._write_alignment_result_txt(detections, raw_transforms, display_camera_transforms, trajectory_anchor_transforms, anchor_quality)

    def _serialize_transform(self, transform: np.ndarray) -> List[List[float]]:
        return self._alignment_controller_instance().persistence._serialize_transform(transform)

    def _deserialize_transform(self, value: object) -> Optional[np.ndarray]:
        return self._alignment_controller_instance().persistence._deserialize_transform(value)

    def _persist_alignment_state(self) -> None:
        return self._alignment_controller_instance().persistence._persist_alignment_state()

    def _load_persisted_alignment_state(self) -> None:
        return self._alignment_controller_instance().persistence._load_persisted_alignment_state()

    def _decode_calibration_message(self, topic_type: str, msg: object) -> Optional[np.ndarray]:
        return self._alignment_controller_instance().detector._decode_calibration_message(topic_type, msg)

    def alignment_status_text(self) -> str:
        return self._alignment_controller_instance().diagnostics.alignment_status_text()
