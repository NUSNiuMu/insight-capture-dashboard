"""Bounded local stereo bundle adjustment for the Insight9 sparse mapper.

The real-time mapper triangulates every stereo pair independently. This module
associates repeated SuperPoint descriptors across recent keyframes, then jointly
refines keyframe poses and one shared 3D position per track. It is deliberately
free of ROS state so the expensive solve can run on a background snapshot.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy.optimize import least_squares
from scipy.sparse import lil_matrix

from .global_localization import match_descriptors, normalize_descriptors
from .pose_graph import se3_exp, se3_log


def _inverse_transform(transform: np.ndarray) -> np.ndarray:
    value = np.asarray(transform, dtype=np.float64).reshape(4, 4)
    result = np.eye(4, dtype=np.float64)
    result[:3, :3] = value[:3, :3].T
    result[:3, 3] = -(result[:3, :3] @ value[:3, 3])
    return result


def _rotation_distance_deg(first: np.ndarray, second: np.ndarray) -> float:
    relative = np.asarray(first)[:3, :3].T @ np.asarray(second)[:3, :3]
    cosine = float(np.clip((np.trace(relative) - 1.0) * 0.5, -1.0, 1.0))
    return math.degrees(math.acos(cosine))


@dataclass(frozen=True)
class BundleAdjustmentConfig:
    """Association, solver and safety limits for one local BA window."""

    min_keyframes: int = 4
    min_track_observations: int = 3
    min_landmarks: int = 20
    max_points_per_keyframe: int = 300
    max_candidate_tracks: int = 2_000
    max_landmarks: int = 400
    association_radius_m: float = 0.12
    descriptor_ratio_test: float = 0.80
    descriptor_min_similarity: float = 0.78
    odometry_translation_std_m: float = 0.03
    odometry_rotation_std_deg: float = 1.0
    robust_loss_px: float = 2.0
    max_iterations: int = 15
    min_relative_improvement: float = 0.005
    max_pose_translation_correction_m: float = 0.10
    max_pose_rotation_correction_deg: float = 5.0
    max_landmark_correction_m: float = 0.20

    def __post_init__(self) -> None:
        if self.min_keyframes < 2 or self.min_track_observations < 2:
            raise ValueError("bundle adjustment needs multiple frames and observations")
        counts = (
            self.min_landmarks,
            self.max_points_per_keyframe,
            self.max_candidate_tracks,
            self.max_landmarks,
            self.max_iterations,
        )
        if any(value <= 0 for value in counts):
            raise ValueError("bundle adjustment resource limits must be positive")
        if self.min_landmarks > self.max_landmarks:
            raise ValueError("minimum landmarks cannot exceed the landmark limit")
        positive = (
            self.association_radius_m,
            self.descriptor_ratio_test,
            self.descriptor_min_similarity,
            self.odometry_translation_std_m,
            self.odometry_rotation_std_deg,
            self.robust_loss_px,
            self.max_pose_translation_correction_m,
            self.max_pose_rotation_correction_deg,
            self.max_landmark_correction_m,
        )
        if any(not np.isfinite(value) or value <= 0.0 for value in positive):
            raise ValueError("bundle adjustment thresholds must be finite and positive")
        if not 0.0 <= self.min_relative_improvement < 1.0:
            raise ValueError("minimum relative improvement must be in [0, 1)")


@dataclass(frozen=True)
class BundleAdjustmentFrame:
    """One immutable keyframe snapshot consumed by local BA."""

    keyframe_id: int
    pose: np.ndarray
    points_left: np.ndarray
    left_pixels: np.ndarray
    right_pixels: np.ndarray
    descriptors: np.ndarray
    scores: np.ndarray

    def __post_init__(self) -> None:
        pose = np.asarray(self.pose)
        points = np.asarray(self.points_left)
        left = np.asarray(self.left_pixels)
        right = np.asarray(self.right_pixels)
        descriptors = np.asarray(self.descriptors)
        scores = np.asarray(self.scores)
        count = len(points)
        if pose.shape != (4, 4) or not np.isfinite(pose).all():
            raise ValueError("bundle adjustment frame pose must be a finite 4x4 matrix")
        if points.shape != (count, 3):
            raise ValueError("points_left must have shape N x 3")
        if left.shape != (count, 2) or right.shape != (count, 2):
            raise ValueError("stereo pixels must have shape N x 2")
        if descriptors.ndim != 2 or descriptors.shape[0] != count:
            raise ValueError("descriptors must have shape N x D")
        if scores.reshape(-1).shape != (count,):
            raise ValueError("scores must have one value per observation")


@dataclass(frozen=True)
class BundleAdjustmentResult:
    """Accepted solution or an explicit reason why a window was not changed."""

    optimized: bool
    success: bool
    reason: str
    keyframes: int
    landmarks: int
    observations: int
    initial_reprojection_rmse_px: float
    final_reprojection_rmse_px: float
    elapsed_ms: float
    max_pose_translation_correction_m: float
    max_pose_rotation_correction_deg: float
    max_landmark_correction_m: float
    poses: dict[int, np.ndarray]
    refined_points_left: dict[int, np.ndarray]


@dataclass
class _Track:
    position_sum: np.ndarray
    descriptor_sum: np.ndarray
    score_sum: float
    observations: list[tuple[int, int]]

    @property
    def position(self) -> np.ndarray:
        return self.position_sum / len(self.observations)

    @property
    def mean_score(self) -> float:
        return self.score_sum / len(self.observations)

    @property
    def descriptor(self) -> np.ndarray:
        norm = float(np.linalg.norm(self.descriptor_sum))
        return (
            self.descriptor_sum / norm
            if norm > 1e-12
            else self.descriptor_sum
        )


def _empty_result(
    reason: str, started: float, keyframes: int
) -> BundleAdjustmentResult:
    return BundleAdjustmentResult(
        optimized=False,
        success=False,
        reason=reason,
        keyframes=keyframes,
        landmarks=0,
        observations=0,
        initial_reprojection_rmse_px=0.0,
        final_reprojection_rmse_px=0.0,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
        max_pose_translation_correction_m=0.0,
        max_pose_rotation_correction_deg=0.0,
        max_landmark_correction_m=0.0,
        poses={},
        refined_points_left={},
    )


def _selected_indices(frame: BundleAdjustmentFrame, limit: int) -> np.ndarray:
    finite = (
        np.isfinite(frame.points_left).all(axis=1)
        & np.isfinite(frame.left_pixels).all(axis=1)
        & np.isfinite(frame.right_pixels).all(axis=1)
        & np.isfinite(frame.descriptors).all(axis=1)
        & np.isfinite(frame.scores).reshape(-1)
    )
    indices = np.flatnonzero(finite)
    if len(indices) <= limit:
        return indices
    scores = np.asarray(frame.scores, dtype=np.float64).reshape(-1)[indices]
    selected = np.argpartition(-scores, kth=limit - 1)[:limit]
    return np.sort(indices[selected])


def _associate_tracks(
    frames: list[BundleAdjustmentFrame], config: BundleAdjustmentConfig
) -> list[_Track]:
    tracks: list[_Track] = []
    for frame_index, frame in enumerate(frames):
        indices = _selected_indices(frame, config.max_points_per_keyframe)
        if len(indices) == 0:
            continue
        points_world = (
            frame.points_left[indices] @ frame.pose[:3, :3].T + frame.pose[:3, 3]
        )
        descriptors = normalize_descriptors(frame.descriptors[indices])
        matched_query: set[int] = set()
        if len(tracks) >= 2:
            track_descriptors = np.asarray(
                [track.descriptor for track in tracks], dtype=np.float32
            )
            query_indices, track_indices, _ = match_descriptors(
                descriptors,
                track_descriptors,
                ratio_test=config.descriptor_ratio_test,
                min_similarity=config.descriptor_min_similarity,
                map_descriptors_normalized=True,
            )
            for query_index, track_index in zip(query_indices, track_indices):
                track = tracks[int(track_index)]
                point = points_world[int(query_index)]
                if np.linalg.norm(point - track.position) > config.association_radius_m:
                    continue
                source_index = int(indices[int(query_index)])
                track.position_sum += point
                track.descriptor_sum += descriptors[int(query_index)]
                track.score_sum += float(
                    np.asarray(frame.scores).reshape(-1)[source_index]
                )
                track.observations.append((frame_index, source_index))
                matched_query.add(int(query_index))

        for query_index, source_index in enumerate(indices):
            if query_index in matched_query:
                continue
            if len(tracks) >= config.max_candidate_tracks:
                break
            tracks.append(
                _Track(
                    position_sum=points_world[query_index].astype(
                        np.float64, copy=True
                    ),
                    descriptor_sum=descriptors[query_index].astype(
                        np.float32, copy=True
                    ),
                    score_sum=float(np.asarray(frame.scores).reshape(-1)[source_index]),
                    observations=[(frame_index, int(source_index))],
                )
            )

    repeated = [
        track
        for track in tracks
        if len(track.observations) >= config.min_track_observations
    ]
    repeated.sort(
        key=lambda track: (len(track.observations), track.mean_score), reverse=True
    )
    return repeated[: config.max_landmarks]


def optimize_local_bundle(
    frames: list[BundleAdjustmentFrame],
    left_projection: np.ndarray,
    right_projection: np.ndarray,
    config: Optional[BundleAdjustmentConfig] = None,
) -> BundleAdjustmentResult:
    """Associate and jointly optimize one bounded recent-keyframe snapshot."""

    started = time.perf_counter()
    config = config or BundleAdjustmentConfig()
    frames = list(frames)
    if len(frames) < config.min_keyframes:
        return _empty_result("insufficient_keyframes", started, len(frames))
    if any(
        frames[index].keyframe_id >= frames[index + 1].keyframe_id
        for index in range(len(frames) - 1)
    ):
        raise ValueError("bundle adjustment keyframes must be strictly ordered")
    left_projection = np.asarray(left_projection, dtype=np.float64).reshape(3, 4)
    right_projection = np.asarray(right_projection, dtype=np.float64).reshape(3, 4)
    tracks = _associate_tracks(frames, config)
    if len(tracks) < config.min_landmarks:
        return _empty_result("insufficient_repeated_landmarks", started, len(frames))

    observations: list[tuple[int, int, int]] = []
    for landmark_index, track in enumerate(tracks):
        observations.extend(
            (frame_index, point_index, landmark_index)
            for frame_index, point_index in track.observations
        )
    observation_frames = np.asarray(
        [entry[0] for entry in observations], dtype=np.int64
    )
    observation_landmarks = np.asarray(
        [entry[2] for entry in observations], dtype=np.int64
    )
    measured_pixels = np.asarray(
        [
            np.concatenate(
                (
                    frames[frame_index].left_pixels[point_index],
                    frames[frame_index].right_pixels[point_index],
                )
            )
            for frame_index, point_index, _ in observations
        ],
        dtype=np.float64,
    )
    frame_observations = [
        np.flatnonzero(observation_frames == frame_index)
        for frame_index in range(len(frames))
    ]
    pose_variable_count = (len(frames) - 1) * 6
    landmark_offset = pose_variable_count
    initial_landmarks = np.asarray(
        [track.position for track in tracks], dtype=np.float64
    )
    initial = np.zeros((pose_variable_count + len(tracks) * 3,), dtype=np.float64)
    initial[landmark_offset:] = initial_landmarks.reshape(-1)
    base_poses = [np.asarray(frame.pose, dtype=np.float64).copy() for frame in frames]
    odometry = [
        _inverse_transform(base_poses[index]) @ base_poses[index + 1]
        for index in range(len(frames) - 1)
    ]

    def unpack(values: np.ndarray) -> tuple[list[np.ndarray], np.ndarray]:
        poses = [base_poses[0]]
        for frame_index in range(1, len(frames)):
            offset = (frame_index - 1) * 6
            poses.append(base_poses[frame_index] @ se3_exp(values[offset : offset + 6]))
        landmarks = values[landmark_offset:].reshape(-1, 3)
        return poses, landmarks

    observation_rows = len(observations) * 4
    total_rows = observation_rows + (len(frames) - 1) * 6

    def residuals(values: np.ndarray) -> np.ndarray:
        poses, landmarks = unpack(values)
        output = np.empty((total_rows,), dtype=np.float64)
        pixel_residuals = output[:observation_rows].reshape(-1, 4)
        for frame_index, indices in enumerate(frame_observations):
            if len(indices) == 0:
                continue
            pose = poses[frame_index]
            points_left = (
                landmarks[observation_landmarks[indices]] - pose[:3, 3]
            ) @ pose[:3, :3]
            homogeneous = np.concatenate(
                (points_left, np.ones((len(indices), 1), dtype=np.float64)), axis=1
            )
            projected_left = homogeneous @ left_projection.T
            projected_right = homogeneous @ right_projection.T
            valid = (projected_left[:, 2] > 1e-5) & (
                projected_right[:, 2] > 1e-5
            )
            projected = np.full((len(indices), 4), 1e3, dtype=np.float64)
            projected[valid, :2] = (
                projected_left[valid, :2] / projected_left[valid, 2:3]
            )
            projected[valid, 2:] = (
                projected_right[valid, :2] / projected_right[valid, 2:3]
            )
            pixel_residuals[indices] = projected - measured_pixels[indices]
        translation_scale = 1.0 / config.odometry_translation_std_m
        rotation_scale = 1.0 / math.radians(config.odometry_rotation_std_deg)
        for edge_index, measured in enumerate(odometry):
            predicted = _inverse_transform(poses[edge_index]) @ poses[edge_index + 1]
            error = se3_log(_inverse_transform(measured) @ predicted)
            row = observation_rows + edge_index * 6
            output[row : row + 3] = error[:3] * translation_scale
            output[row + 3 : row + 6] = error[3:] * rotation_scale
        return output

    sparsity = lil_matrix((total_rows, len(initial)), dtype=np.int8)
    for observation_index, (frame_index, _, landmark_index) in enumerate(observations):
        row = observation_index * 4
        if frame_index > 0:
            pose_offset = (frame_index - 1) * 6
            sparsity[row : row + 4, pose_offset : pose_offset + 6] = 1
        point_offset = landmark_offset + landmark_index * 3
        sparsity[row : row + 4, point_offset : point_offset + 3] = 1
    for edge_index in range(len(frames) - 1):
        row = observation_rows + edge_index * 6
        for frame_index in (edge_index, edge_index + 1):
            if frame_index == 0:
                continue
            pose_offset = (frame_index - 1) * 6
            sparsity[row : row + 6, pose_offset : pose_offset + 6] = 1

    initial_residuals = residuals(initial)
    initial_rmse = float(
        np.sqrt(np.mean(initial_residuals[:observation_rows] ** 2))
    )
    solution = least_squares(
        residuals,
        initial,
        jac_sparsity=sparsity.tocsr(),
        method="trf",
        loss="huber",
        f_scale=config.robust_loss_px,
        x_scale="jac",
        max_nfev=config.max_iterations,
        ftol=1e-5,
        xtol=1e-5,
        gtol=1e-5,
    )
    optimized_poses, optimized_landmarks = unpack(solution.x)
    final_residuals = residuals(solution.x)
    final_rmse = float(np.sqrt(np.mean(final_residuals[:observation_rows] ** 2)))
    relative_improvement = (initial_rmse - final_rmse) / max(initial_rmse, 1e-9)
    pose_translation = max(
        float(np.linalg.norm(after[:3, 3] - before[:3, 3]))
        for before, after in zip(base_poses, optimized_poses)
    )
    pose_rotation = max(
        _rotation_distance_deg(before, after)
        for before, after in zip(base_poses, optimized_poses)
    )
    landmark_correction = float(
        np.max(np.linalg.norm(optimized_landmarks - initial_landmarks, axis=1))
    )
    finite = bool(
        np.isfinite(solution.x).all()
        and np.isfinite(final_rmse)
        and all(np.isfinite(pose).all() for pose in optimized_poses)
    )
    reason = "optimized"
    accepted = True
    if not finite:
        reason, accepted = "non_finite_solution", False
    elif (
        final_rmse >= initial_rmse
        or relative_improvement < config.min_relative_improvement
    ):
        reason, accepted = "insufficient_improvement", False
    elif pose_translation > config.max_pose_translation_correction_m:
        reason, accepted = "pose_translation_gate", False
    elif pose_rotation > config.max_pose_rotation_correction_deg:
        reason, accepted = "pose_rotation_gate", False
    elif landmark_correction > config.max_landmark_correction_m:
        reason, accepted = "landmark_correction_gate", False

    refined_points: dict[int, np.ndarray] = {}
    poses_by_id: dict[int, np.ndarray] = {}
    if accepted:
        for frame, pose in zip(frames, optimized_poses):
            poses_by_id[frame.keyframe_id] = pose.copy()
            refined_points[frame.keyframe_id] = np.asarray(
                frame.points_left, dtype=np.float32
            ).copy()
        for landmark_index, track in enumerate(tracks):
            point_world = optimized_landmarks[landmark_index]
            for frame_index, point_index in track.observations:
                pose = optimized_poses[frame_index]
                point_left = pose[:3, :3].T @ (point_world - pose[:3, 3])
                if point_left[2] > 1e-4 and np.isfinite(point_left).all():
                    refined_points[frames[frame_index].keyframe_id][
                        point_index
                    ] = point_left

    return BundleAdjustmentResult(
        optimized=accepted,
        # Reaching the bounded evaluation limit is expected online; a finite,
        # gated cost reduction is still a successful usable solution.
        success=bool(accepted and finite),
        reason=reason,
        keyframes=len(frames),
        landmarks=len(tracks),
        observations=len(observations),
        initial_reprojection_rmse_px=initial_rmse,
        final_reprojection_rmse_px=final_rmse,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
        max_pose_translation_correction_m=pose_translation,
        max_pose_rotation_correction_deg=pose_rotation,
        max_landmark_correction_m=landmark_correction,
        poses=poses_by_id,
        refined_points_left=refined_points,
    )
