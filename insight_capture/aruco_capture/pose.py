"""Direct cube observations in one recording frame; no gripper odometry."""

from dataclasses import replace

import cv2
import numpy as np

from insight_capture.runtime.mapping.cube_markers import (
    CubePoseEstimate,
    MultiCubeMarkerEstimator,
    load_cube_marker_config,
)
from insight_capture.runtime.mapping.geometry import (
    matrix_from_pose,
    matrix_from_transform,
    rotation_distance_deg,
)
from insight_capture.runtime.mapping.synchronization import PoseBuffer


class DirectPose(MultiCubeMarkerEstimator):
    def __init__(self, path, config):
        super().__init__(
            replace(
                load_cube_marker_config(path), apply_corrections=False, min_markers=1
            )
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
            return None
        candidates = []
        for rvec, tvec in zip(rotations, translations):
            rotation = cv2.Rodrigues(rvec)[0]
            depth = (objects @ rotation.T + tvec.reshape(1, 3))[:, 2]
            if (
                depth.min() < self.config.min_depth_m
                or depth.max() > self.config.max_depth_m
            ):
                continue
            projected = cv2.projectPoints(objects, rvec, tvec, intrinsic, None)[
                0
            ].reshape(-1, 2)
            error = np.linalg.norm(projected - pixels, axis=1)
            if (
                np.median(error) > self.config.max_reprojection_error_px
                or error.max() > 2 * self.config.max_reprojection_error_px
            ):
                continue
            transform = np.eye(4)
            transform[:3, :3], transform[:3, 3] = rotation, tvec.reshape(3)
            candidates.append((float(np.median(error)), float(error.max()), transform))
        if not candidates:
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

    def observe(self, gray, intrinsic, head_pose, imu_from_rgb, seconds):
        world_from_imu = matrix_from_pose(head_pose)
        self.world_from_rgb = world_from_imu @ imu_from_rgb
        self.time = seconds
        if self.reference is None:
            self.reference = np.linalg.inv(world_from_imu)
        estimates = self.detect(gray, intrinsic)
        result = {}
        for name in self.config.targets:
            estimate = estimates.get(name)
            if estimate is None:
                result[name] = {"valid": False, "reason": "marker_not_detected"}
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
