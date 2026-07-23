"""AprilTag board pose solving and calibration image decoding."""

from __future__ import annotations

import math
from typing import Optional, Tuple

import cv2
import numpy as np
from sensor_msgs.msg import Image as RosImage


class AlignmentDetector:
    def __init__(self, owner) -> None:
        self.owner = owner

    def _solve_board_pose(
        self,
        corners,
        ids,
        camera_matrix: np.ndarray,
        dist_coeffs: np.ndarray,
    ) -> Optional[Tuple[np.ndarray, np.ndarray, Optional[float]]]:
        """Solve board->camera pose. Returns (rvec, tvec, rms_reproj_px or None).

        RANSAC over the matched board corners (a single mis-detected tag no
        longer skews the whole solution), LM refinement on the inliers, and an
        RMS reprojection error the caller gates on. Falls back to
        estimatePoseBoard (no reprojection metric) on OpenCV builds without
        GridBoard.matchImagePoints.
        """
        board = self.owner.live_alignment_board
        if not hasattr(board, "matchImagePoints"):
            return self.owner._solve_board_pose_legacy(corners, ids, camera_matrix, dist_coeffs)
        obj_points, img_points = board.matchImagePoints(corners, ids)
        if obj_points is None or img_points is None or len(obj_points) < 8:
            return None
        obj = np.ascontiguousarray(obj_points.reshape(-1, 3), dtype=np.float64)
        img = np.ascontiguousarray(img_points.reshape(-1, 2), dtype=np.float64)
        try:
            ok, rvec, tvec, inlier_idx = cv2.solvePnPRansac(
                obj,
                img,
                camera_matrix,
                dist_coeffs,
                reprojectionError=self.owner.live_alignment_max_reprojection_error_px,
                flags=cv2.SOLVEPNP_ITERATIVE,
            )
        except cv2.error:
            return None
        # Half the corners of the min tag count may be RANSAC-rejected before
        # the frame itself is considered unusable (4 corners per tag).
        min_corner_inliers = 2 * self.owner.live_alignment_min_detected_tags
        if not ok or rvec is None or tvec is None or inlier_idx is None or len(inlier_idx) < min_corner_inliers:
            return None
        inlier_idx = inlier_idx.reshape(-1)
        obj_in = obj[inlier_idx]
        img_in = img[inlier_idx]
        # Planar boards have a two-fold pose ambiguity; the iterative solver
        # returns only one branch and at oblique view angles it frequently
        # lands in the wrong one (verified by Monte-Carlo sweep: >50% flipped
        # poses at 55deg tilt, and the flipped pose still passes the
        # reprojection gate). IPPE returns BOTH branches, so re-solve on the
        # RANSAC inliers with IPPE, pick the branch by residual, and drop the
        # frame entirely when the two branches are rotationally distinct yet
        # fit equally well (genuinely ambiguous view).
        resolved = self.owner._disambiguate_planar_pose(obj_in, img_in, camera_matrix, dist_coeffs)
        if resolved is None:
            return None
        rvec, tvec = resolved
        try:
            rvec, tvec = cv2.solvePnPRefineLM(obj_in, img_in, camera_matrix, dist_coeffs, rvec, tvec)
        except cv2.error:
            pass
        projected, _ = cv2.projectPoints(obj_in, rvec, tvec, camera_matrix, dist_coeffs)
        residuals = projected.reshape(-1, 2) - img_in
        rms_px = float(np.sqrt(np.mean(np.sum(residuals * residuals, axis=1))))
        return rvec, tvec, rms_px

    def _disambiguate_planar_pose(
        self,
        obj_in: np.ndarray,
        img_in: np.ndarray,
        camera_matrix: np.ndarray,
        dist_coeffs: np.ndarray,
    ) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        """Pick the correct branch of the planar two-fold pose ambiguity.

        Returns (rvec, tvec) of the better IPPE branch, or None when the view
        is genuinely ambiguous (both branches fit within the reprojection gate
        but disagree in rotation) so the caller skips the frame.
        """
        try:
            solution_count, rvecs, tvecs, errors = cv2.solvePnPGeneric(
                obj_in.reshape(-1, 1, 3),
                img_in.reshape(-1, 1, 2),
                camera_matrix,
                dist_coeffs,
                flags=cv2.SOLVEPNP_IPPE,
            )
        except cv2.error:
            solution_count = 0
        if not solution_count:
            return None
        errors = np.asarray(errors, dtype=np.float64).reshape(-1)[:solution_count]
        order = np.argsort(errors)
        best = int(order[0])
        if solution_count > 1:
            second = int(order[1])
            rotation_best, _ = cv2.Rodrigues(rvecs[best])
            rotation_second, _ = cv2.Rodrigues(rvecs[second])
            trace = float(np.trace(rotation_best @ rotation_second.T))
            cos_theta = max(-1.0, min(1.0, (trace - 1.0) * 0.5))
            branch_gap_deg = math.degrees(math.acos(cos_theta))
            gate = self.owner.live_alignment_max_reprojection_error_px
            ratio = errors[second] / max(errors[best], 1e-9)
            if branch_gap_deg > 20.0 and errors[second] <= gate and ratio < 1.4:
                return None
        return rvecs[best], tvecs[best]

    def _solve_board_pose_legacy(
        self,
        corners,
        ids,
        camera_matrix: np.ndarray,
        dist_coeffs: np.ndarray,
    ) -> Optional[Tuple[np.ndarray, np.ndarray, Optional[float]]]:
        try:
            estimate = cv2.aruco.estimatePoseBoard(
                corners, ids, self.owner.live_alignment_board, camera_matrix, dist_coeffs, None, None
            )
        except TypeError:
            estimate = cv2.aruco.estimatePoseBoard(
                corners, ids, self.owner.live_alignment_board, camera_matrix, dist_coeffs
            )
        except cv2.error:
            return None
        if isinstance(estimate, tuple):
            if len(estimate) == 3:
                retval, rvec, tvec = estimate
            else:
                _, rvec, tvec = estimate
                retval = 0 if rvec is None or tvec is None else len(ids)
        else:
            return None
        if retval is None or float(retval) <= 0.0 or rvec is None or tvec is None:
            return None
        return rvec, tvec, None

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
        if encoding == "nv12" and msg.width > 0:
            # Some drivers report msg.height as the full Y+UV buffer height
            # rather than the displayed luma height, so derive the real
            # buffer shape from the data length instead of trusting msg.height.
            total_rows, remainder = divmod(data.size, msg.width)
            if remainder == 0 and total_rows > 0 and total_rows % 3 == 0:
                yuv = data.reshape((total_rows, msg.width))
                return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_NV12)
            if remainder == 0 and total_rows > 0:
                gray = data.reshape((total_rows, msg.width))
                return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        return None
