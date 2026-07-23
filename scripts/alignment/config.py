"""Online alignment configuration and initial state."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from session_alignment import matrix_to_transform

from .state import DetectionSample


class AlignmentConfigurator:
    def __init__(self, owner) -> None:
        self.owner = owner

    def _configure_live_alignment(self, raw_config: Dict, config: Dict) -> None:
        alignment_config = config.get("session_alignment", {})
        self.owner.session_alignment_enabled = bool(alignment_config.get("enabled", False))
        self.owner.reference_camera = alignment_config.get("reference_camera")
        self.owner.live_alignment_frame = str(alignment_config.get("alignment_frame", "board_center") or "board_center")
        self.owner.world_to_reference = {}
        calibration_config = raw_config.get("session_alignment", {}).get("calibration", {})
        self.owner.live_alignment_available = self.owner.session_alignment_enabled and hasattr(cv2, "aruco")
        self.owner.live_alignment_active = False
        self.owner.live_alignment_image_stream = str(calibration_config.get("image_stream", "color") or "color")
        self.owner.live_alignment_method = str(calibration_config.get("method", "board_center") or "board_center")
        if self.owner.live_alignment_method == "board_relative":
            self.owner.live_alignment_method = "board_center"
        if self.owner.live_alignment_frame == "board_relative":
            self.owner.live_alignment_frame = "board_center"
        self.owner.live_alignment_required_samples = int(calibration_config.get("required_samples", 12))
        self.owner.live_alignment_window = max(
            int(calibration_config.get("stability_window", 20)),
            self.owner.live_alignment_required_samples,
        )
        self.owner.live_alignment_min_detected_tags = max(2, int(calibration_config.get("min_detected_tags", 4)))
        self.owner.live_alignment_max_group_span_ns = int(float(calibration_config.get("max_group_span_ms", 180.0)) * 1_000_000)
        self.owner.live_alignment_pending_image_limit = max(2, int(calibration_config.get("pending_image_limit", 8)))
        self.owner.live_alignment_pending_max_age_ns = int(float(calibration_config.get("pending_max_age_ms", 500.0)) * 1_000_000)
        self.owner.live_alignment_detection_buffer_limit = max(
            3,
            int(calibration_config.get("detection_buffer_limit", 20)),
        )
        self.owner.live_alignment_detection_max_age_ns = int(
            float(calibration_config.get("detection_max_age_ms", 500.0)) * 1_000_000
        )
        # Uncapped above 1.0: mono/IR streams can have markers only ~40-50px
        # wide at native resolution (too small for reliable quad extraction),
        # and upscaling before detection measurably recovers detections in
        # that case (verified against real insight3 footage).
        self.owner.live_alignment_image_scale = max(
            0.1,
            float(calibration_config.get("alignment_image_scale", 1.0)),
        )
        self.owner.live_alignment_processing_hz = max(
            0.5,
            min(30.0, float(calibration_config.get("processing_hz", 10.0))),
        )
        self.owner.live_alignment_display_axis_alignment = bool(
            calibration_config.get("display_axis_alignment", True)
        )
        self.owner.live_alignment_dashboard_pose_max_age_ns = int(
            float(calibration_config.get("dashboard_pose_max_age_ms", 150.0)) * 1_000_000
        )
        self.owner.live_alignment_dashboard_horizontal_yaw_mode = str(
            calibration_config.get("dashboard_horizontal_yaw_mode", "manual") or "manual"
        )
        self.owner.live_alignment_dashboard_horizontal_yaw_deg = float(
            calibration_config.get("dashboard_horizontal_yaw_deg", 0.0)
        )
        self.owner.live_alignment_lock_on_first_solution = bool(
            calibration_config.get("lock_on_first_solution", True)
        )
        self.owner.live_alignment_reset_traces_on_lock = bool(
            calibration_config.get("reset_traces_on_lock", True)
        )
        self.owner.live_alignment_anchor_rotation_mode = str(
            calibration_config.get("anchor_rotation_mode", "yaw") or "yaw"
        ).lower()
        if self.owner.live_alignment_anchor_rotation_mode not in {"none", "yaw", "full"}:
            self.owner.live_alignment_anchor_rotation_mode = "yaw"
        # Per-frame detection quality gate: RMS reprojection error (px) of the
        # RANSAC-inlier board corners. Frames above this are dropped before
        # they ever become anchor candidates.
        self.owner.live_alignment_max_reprojection_error_px = float(
            calibration_config.get("max_reprojection_error_px", 2.0)
        )
        # Anchor-candidate gates. These replace the old max_translation_std_m /
        # max_rotation_std_deg board-pose-scatter gates: board->camera scatter
        # conflates camera motion with noise (forcing a stay-still calibration),
        # whereas the anchor is constant under motion, so its spread measures
        # actual solution quality. Used both as the MAD inlier floor and as a
        # hard RMS ceiling below which a solution may be published.
        self.owner.live_alignment_anchor_max_translation_std_m = float(
            calibration_config.get("anchor_max_translation_std_m", 0.05)
        )
        self.owner.live_alignment_anchor_max_rotation_std_deg = float(
            calibration_config.get("anchor_max_rotation_std_deg", 3.0)
        )
        self.owner.live_alignment_last_status = "live alignment idle"
        self.owner.live_alignment_last_signature: Optional[Tuple[int, ...]] = None
        self.owner.live_alignment_visible_cameras: int = 0
        self.owner.live_alignment_inlier_counts: Dict[str, int] = {}
        self.owner.live_alignment_target_camera: Optional[str] = None
        self.owner.live_alignment_logged_status: Optional[str] = None
        self.owner.live_alignment_last_sync_span_ms: float = 0.0
        self.owner.live_alignment_last_transform_summary: Dict[str, str] = {}
        self.owner.live_alignment_last_raw_transform_summary: Dict[str, str] = {}
        self.owner.live_alignment_last_anchor_summary: Dict[str, str] = {}
        self.owner.live_alignment_last_tag_count: Dict[str, int] = {
            camera["name"]: 0
            for camera in raw_config.get("cameras", [])
            if camera.get("enabled", True)
        }
        self.owner.live_alignment_last_summary_time: float = 0.0
        self.owner.live_alignment_result_txt_path = Path(
            os.environ.get("INSIGHT_ALIGNMENT_RESULT", "/tmp/insight_live_alignment_result.txt")
        )
        default_state_path = Path(__file__).resolve().parents[2] / "config" / "alignment" / "live_alignment_state.json"
        self.owner.live_alignment_state_path = Path(
            os.environ.get("INSIGHT_ALIGNMENT_STATE", str(default_state_path))
        )
        self.owner.live_alignment_debug_state: Dict[str, Dict[str, object]] = {}

        dictionary_name = str(calibration_config.get("dictionary", "DICT_APRILTAG_36h11"))
        dictionary_id = getattr(cv2.aruco, dictionary_name)
        self.owner.live_alignment_dictionary_name = dictionary_name
        self.owner.live_alignment_aruco_dict = cv2.aruco.getPredefinedDictionary(dictionary_id)
        self.owner.live_alignment_detector = None
        detector_params = cv2.aruco.DetectorParameters()
        # Default is CORNER_REFINE_NONE (coarse polygon-approximation corners).
        # CORNER_REFINE_APRILTAG gives the best corners on clean/high-res
        # images but was verified (against real insight3 mono footage where
        # markers are only ~40-50px wide) to detect *zero* markers where NONE
        # and SUBPIX both still work — its internal refinement apparently
        # needs more pixels-per-module than this fleet's cameras provide.
        # SUBPIX is the safer middle ground: still meaningfully better than
        # NONE on well-resolved images, without APRILTAG's total failure mode
        # on marginal-resolution ones.
        detector_params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
        if hasattr(cv2.aruco, "ArucoDetector"):
            self.owner.live_alignment_detector = cv2.aruco.ArucoDetector(
                self.owner.live_alignment_aruco_dict,
                detector_params,
            )

        board_rows = int(calibration_config.get("board_rows", 6))
        board_cols = int(calibration_config.get("board_cols", 6))
        marker_length_m = float(calibration_config.get("marker_length_m", 0.055))
        marker_separation_m = float(calibration_config.get("marker_separation_m", 0.0165))
        self.owner.live_alignment_board_width_m = board_cols * marker_length_m + (board_cols - 1) * marker_separation_m
        self.owner.live_alignment_board_height_m = board_rows * marker_length_m + (board_rows - 1) * marker_separation_m
        self.owner.live_alignment_board_center_offset = matrix_to_transform(
            np.eye(3, dtype=np.float64),
            np.array(
                [
                    self.owner.live_alignment_board_width_m * 0.5,
                    self.owner.live_alignment_board_height_m * 0.5,
                    0.0,
                ],
                dtype=np.float64,
            ),
        )
        if hasattr(cv2.aruco, "GridBoard"):
            grid_board = cv2.aruco.GridBoard(
                (board_cols, board_rows),
                marker_length_m,
                marker_separation_m,
                self.owner.live_alignment_aruco_dict,
            )
        else:
            grid_board = cv2.aruco.GridBoard_create(
                board_cols,
                board_rows,
                marker_length_m,
                marker_separation_m,
                self.owner.live_alignment_aruco_dict,
            )
        # The physical board in this fleet has its ids running RIGHT-TO-LEFT
        # within each row (relative to the tags' own orientation), i.e. a
        # row-mirrored version of what GridBoard assumes. A mirrored id layout
        # cannot be fit by any rigid pose, so with the standard board every
        # solver "converges" to a garbage pose (~110px reprojection residual,
        # verified against a live insight7_b frame on 2026-07-09) -- which is
        # what the old, gate-less estimatePoseBoard silently produced.
        board_id_layout = str(calibration_config.get("board_id_layout", "standard")).lower()
        if board_id_layout == "row_mirrored" and hasattr(grid_board, "getObjPoints"):
            grid_obj = grid_board.getObjPoints()
            marker_count = board_rows * board_cols
            mirrored = [
                grid_obj[(index // board_cols) * board_cols + (board_cols - 1 - index % board_cols)]
                for index in range(marker_count)
            ]
            self.owner.live_alignment_board = cv2.aruco.Board(
                mirrored,
                self.owner.live_alignment_aruco_dict,
                np.arange(marker_count).astype(np.int32),
            )
        else:
            if board_id_layout not in ("standard", "row_mirrored"):
                print(f"[alignment] unknown board_id_layout '{board_id_layout}', using standard", flush=True)
            self.owner.live_alignment_board = grid_board

    def _initialize_live_alignment_state(self) -> None:
        self.owner.live_alignment_camera_matrix: Dict[str, Optional[np.ndarray]] = {
            camera.name: None for camera in self.owner.cameras
        }
        self.owner.live_alignment_dist_coeffs: Dict[str, Optional[np.ndarray]] = {
            camera.name: None for camera in self.owner.cameras
        }
        self.owner.live_alignment_latest_image: Dict[str, Optional[np.ndarray]] = {
            camera.name: None for camera in self.owner.cameras
        }
        self.owner.live_alignment_latest_image_stamp_ns: Dict[str, int] = {
            camera.name: -1 for camera in self.owner.cameras
        }
        self.owner.live_alignment_processed_stamp_ns: Dict[str, int] = {
            camera.name: -1 for camera in self.owner.cameras
        }
        self.owner.live_alignment_pending_images: Dict[str, List[Tuple[int, int, np.ndarray]]] = {
            camera.name: [] for camera in self.owner.cameras
        }
        self.owner.live_alignment_latest_detection: Dict[str, Optional[DetectionSample]] = {
            camera.name: None for camera in self.owner.cameras
        }
        self.owner.live_alignment_detection_buffer: Dict[str, List[DetectionSample]] = {
            camera.name: [] for camera in self.owner.cameras
        }
        # Per camera: (stamp_ns, board_to_camera, display_transform, anchor_candidate or None)
        self.owner.live_alignment_samples_by_camera: Dict[str, List[Tuple[int, np.ndarray, np.ndarray, Optional[np.ndarray]]]] = {
            camera.name: [] for camera in self.owner.cameras
        }
        self.owner.live_alignment_target_camera = None
        self.owner.live_alignment_topic_by_camera: Dict[str, str] = {}
        self.owner._reset_live_alignment_debug_state()
        self.owner._load_persisted_alignment_state()
