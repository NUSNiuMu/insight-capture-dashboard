"""Direct cube observations in one recording frame; no gripper odometry."""

from dataclasses import replace

import cv2
import numpy as np

from insight_capture.runtime.mapping.cube_markers import (
    CubePoseEstimate,
    MultiCubeMarkerEstimator,
    parse_cube_marker_config,
)
from insight_capture.runtime.mapping.geometry import (
    matrix_from_pose,
    matrix_from_transform,
    rotation_distance_deg,
)
from insight_capture.runtime.mapping.synchronization import PoseBuffer


REFINEMENTS = {
    "none": cv2.aruco.CORNER_REFINE_NONE,
    "subpix": cv2.aruco.CORNER_REFINE_SUBPIX,
    "contour": cv2.aruco.CORNER_REFINE_CONTOUR,
    "apriltag": cv2.aruco.CORNER_REFINE_APRILTAG,
}


def marker_detector(dictionary, refinement):
    """ArUco detector with the named corner refinement.

    Default parameters leave corners unrefined (pixel-quantised), which on a
    ~100 px marker is several degrees of single-face rotation noise.
    "apriltag" is the most precise but costs ~50 ms on a full 1088x1920
    frame, so "roi_apriltag" runs it only on a crop around each marker found
    by a fast unrefined pass."""
    if refinement == "roi_apriltag":
        return RoiAprilTagDetector(dictionary)
    if refinement not in REFINEMENTS:
        raise ValueError(f"unknown corner_refinement: {refinement}")
    parameters = cv2.aruco.DetectorParameters()
    parameters.cornerRefinementMethod = REFINEMENTS[refinement]
    return cv2.aruco.ArucoDetector(dictionary, parameters)


class RoiAprilTagDetector:
    def __init__(self, dictionary, margin=0.6):
        self.coarse = marker_detector(dictionary, "none")
        self.fine = marker_detector(dictionary, "apriltag")
        self.margin = margin

    def detectMarkers(self, image):
        corners, ids, rejected = self.coarse.detectMarkers(image)
        if ids is None:
            return corners, ids, rejected
        height, width = image.shape[:2]
        refined = []
        for corner, marker_id in zip(corners, ids.reshape(-1)):
            corner = corner.reshape(4, 2)
            low, high = corner.min(0), corner.max(0)
            pad = (high - low).max() * self.margin
            x0, y0 = np.maximum(np.floor(low - pad).astype(int), 0)
            x1, y1 = np.minimum(np.ceil(high + pad).astype(int), [width, height])
            fine, fine_ids, _ = self.fine.detectMarkers(image[y0:y1, x0:x1])
            best = corner
            for candidate, candidate_id in zip(
                fine, () if fine_ids is None else fine_ids.reshape(-1)
            ):
                candidate = candidate.reshape(4, 2) + [x0, y0]
                # Refinement moves corners by a pixel or two; anything more is
                # a different quad (or corner order) and is ignored.
                if candidate_id == marker_id and np.abs(candidate - corner).max() < 3:
                    best = candidate
            refined.append(best.reshape(1, 4, 2).astype(np.float32))
        return tuple(refined), ids, rejected


class DirectPose(MultiCubeMarkerEstimator):
    def __init__(self, config, refinement=None):
        super().__init__(
            replace(
                parse_cube_marker_config(config), apply_corrections=False, min_markers=1
            )
        )
        self.refinement = refinement or config[
            "cube_marker_relative_localization"
        ].get("corner_refinement", "none")
        self._detector = marker_detector(
            self._detector.getDictionary(), self.refinement
        )
        self.settings = config
        self.buffer = PoseBuffer()
        self.reference = None
        self.previous = {}
        self.world_from_rgb = np.eye(4)
        self.time = 0.0
        self.generation = 0
        self.last_vio = None
        self.tcp = {
            name: matrix_from_transform(value["translation_m"], value["rotation_xyzw"])
            for name, value in config["tcp"].items()
        }

    def add_vio(self, sample):
        if self.last_vio is not None:
            old = self.last_vio
            dt = (sample.stamp_ns - old.stamp_ns) / 1e9
            if dt < 0 or (
                0 < dt < 0.5
                and (
                    np.linalg.norm(sample.translation - old.translation)
                    > self.settings.get("vio_jump_m", 0.5)
                    or rotation_distance_deg(
                        matrix_from_pose(old)[:3, :3], matrix_from_pose(sample)[:3, :3]
                    )
                    > 60
                )
            ):
                self.generation += 1
                self.buffer.clear()
                self.previous.clear()
        self.last_vio = sample
        self.buffer.append(sample)

    def reset_reference(self):
        self.reference = None
        self.previous.clear()

    def _solve_target(self, camera, marker_ids, objects, pixels, intrinsic):
        if len(marker_ids) != 1:
            return super()._solve_target(camera, marker_ids, objects, pixels, intrinsic)
        ok, rotations, translations, _ = cv2.solvePnPGeneric(
            objects,
            pixels,
            intrinsic,
            None,
            flags=cv2.SOLVEPNP_IPPE,
        )
        if not ok:
            self.last_reasons[camera] = "pnp_solve_failed"
            return None
        candidates = []
        depth_rejected = reprojection_rejected = False
        for rvec, tvec in zip(rotations, translations):
            rotation = cv2.Rodrigues(rvec)[0]
            depth = (objects @ rotation.T + tvec.reshape(1, 3))[:, 2]
            if (
                depth.min() < self.config.min_depth_m
                or depth.max() > self.config.max_depth_m
            ):
                depth_rejected = True
                continue
            projected = cv2.projectPoints(objects, rvec, tvec, intrinsic, None)[
                0
            ].reshape(-1, 2)
            error = np.linalg.norm(projected - pixels, axis=1)
            if (
                np.median(error) > self.config.max_reprojection_error_px
                or error.max() > 2 * self.config.max_reprojection_error_px
            ):
                reprojection_rejected = True
                continue
            transform = np.eye(4)
            transform[:3, :3], transform[:3, 3] = rotation, tvec.reshape(3)
            candidates.append((float(np.median(error)), float(error.max()), transform))
        if not candidates:
            self.last_reasons[camera] = (
                "reprojection_too_high"
                if reprojection_rejected
                else "depth_out_of_range" if depth_rejected else "pnp_solve_failed"
            )
            return None
        best_error = min(x[0] for x in candidates)
        candidates = [x for x in candidates if x[0] <= best_error + 0.3]
        previous = self.previous.get(camera)

        def score(candidate):
            if previous is None or self.time - previous[0] > 0.25:
                return candidate[0]
            world = self.world_from_rgb @ candidate[2]
            return rotation_distance_deg(
                previous[1][:3, :3], world[:3, :3]
            ) + 100 * np.linalg.norm(previous[1][:3, 3] - world[:3, 3])

        median, maximum, transform = min(candidates, key=score)
        return CubePoseEstimate(
            camera, transform, tuple(marker_ids), 4, 4, 1.0, median, maximum
        )

    def markers(self, gray):
        corners, ids, _ = self._detector.detectMarkers(gray)
        if ids is None:
            return {}
        return {
            int(marker_id): np.asarray(corner, np.float64).reshape(4, 2)
            for corner, marker_id in zip(corners, ids.reshape(-1))
        }

    def observe(
        self, gray, intrinsic, head_pose, imu_from_rgb, seconds, detections=None
    ):
        # head_pose is None when VIO is disabled (camera rigidly mounted): the
        # IMU frame is then treated as coinciding with world, so the cube's
        # pose is tracked purely relative to the fixed camera, with no VIO
        # drift/motion correction.
        world_from_imu = np.eye(4) if head_pose is None else matrix_from_pose(head_pose)
        self.world_from_rgb = world_from_imu @ imu_from_rgb
        self.time = seconds
        if self.reference is None:
            self.reference = np.linalg.inv(world_from_imu)
        # Offline recompute detects markers in worker processes and replays
        # them here, so the temporal state below stays sequential.
        estimates = self.estimate(
            self.markers(gray) if detections is None else detections, intrinsic
        )
        result = {}
        for name in self.config.targets:
            estimate = estimates.get(name)
            if estimate is None:
                reason = self.last_reasons.get(name, "marker_not_detected")
                result[name] = {"valid": False, "reason": reason}
                continue
            world_cube = self.world_from_rgb @ estimate.rgb_from_cube
            previous = self.previous.get(name)
            valid, reason = True, "ok"
            if previous is not None and 0 < seconds - previous[0] < 0.25:
                dt = seconds - previous[0]
                distance = np.linalg.norm(world_cube[:3, 3] - previous[1][:3, 3])
                angle = rotation_distance_deg(world_cube[:3, :3], previous[1][:3, :3])
                if (
                    distance > 0.03 + self.settings.get("max_speed_m_s", 3) * dt
                    or angle > 15 + 360 * dt
                ):
                    valid, reason = False, "pose_jump"
            if valid:
                self.previous[name] = (seconds, world_cube)
            tcp = (
                self.reference
                @ world_cube
                @ self.config.targets[name].cube_from_camera_center
                @ self.tcp[name]
            )
            result[name] = {
                "valid": valid,
                "reason": reason,
                "matrix": tcp.tolist(),
                "marker_ids": list(estimate.marker_ids),
                "single_face": len(estimate.marker_ids) == 1,
                "reprojection_px": estimate.median_reprojection_error_px,
            }
        return result
