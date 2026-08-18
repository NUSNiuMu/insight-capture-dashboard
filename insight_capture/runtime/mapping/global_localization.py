"""SuperPoint descriptor-map matching and confirmed 2D-3D localization."""

from __future__ import annotations

import time
from dataclasses import dataclass
from collections import deque
from typing import Optional

import cv2
import numpy as np

from .geometry import rotation_distance_deg


@dataclass(frozen=True)
class GlobalLocalizationConfig:
    """Acceptance and temporal-consistency thresholds for global PnP."""

    ratio_test: float = 0.80
    min_similarity: float = 0.65
    min_matches: int = 12
    min_inliers: int = 10
    min_inlier_ratio: float = 0.45
    max_reprojection_error_px: float = 3.0
    min_grid_cells: int = 4
    confirmation_frames: int = 3
    confirmation_window: int = 5
    confirmation_translation_m: float = 0.20
    confirmation_rotation_deg: float = 12.0


@dataclass(frozen=True)
class LocalizationCandidate:
    """One geometrically accepted map-to-camera and map-to-odom solution."""

    map_to_camera: np.ndarray
    map_to_odom: np.ndarray
    matches: int
    inliers: int
    inlier_ratio: float
    median_reprojection_error_px: float
    grid_cells: int


DESCRIPTOR_QUERY_BLOCK_SIZE = 256


def normalize_descriptors(values: np.ndarray) -> np.ndarray:
    """Return float32 descriptors with unit-length rows."""

    rows = np.asarray(values, dtype=np.float32)
    norms = np.linalg.norm(rows, axis=1, keepdims=True)
    return rows / np.maximum(norms, 1e-12)


def match_descriptors(
    query_descriptors: np.ndarray,
    map_descriptors: np.ndarray,
    *,
    ratio_test: float,
    min_similarity: float,
    map_descriptors_normalized: bool = False,
    query_block_size: int = DESCRIPTOR_QUERY_BLOCK_SIZE,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return mutual query/map indices passing cosine similarity and ratio tests."""

    query = normalize_descriptors(query_descriptors)
    mapped = (
        np.asarray(map_descriptors, dtype=np.float32)
        if map_descriptors_normalized
        else normalize_descriptors(map_descriptors)
    )
    if len(query) == 0 or len(mapped) < 2:
        empty = np.empty((0,), dtype=np.int64)
        return empty, empty, np.empty((0,), dtype=np.float32)
    block_size = int(query_block_size)
    if block_size <= 0:
        raise ValueError("query_block_size must be positive")

    best_map = np.empty((len(query),), dtype=np.int64)
    best_similarity = np.empty((len(query),), dtype=np.float32)
    second_similarity = np.empty((len(query),), dtype=np.float32)
    mutual_best_query = np.zeros((len(mapped),), dtype=np.int64)
    mutual_best_similarity = np.full((len(mapped),), -np.inf, dtype=np.float32)
    map_indices = np.arange(len(mapped), dtype=np.int64)

    # Query blocking bounds the temporary similarity matrix while preserving
    # the exact ratio test and first-query tie behavior of a full matrix.
    for start in range(0, len(query), block_size):
        end = min(start + block_size, len(query))
        similarity = query[start:end] @ mapped.T
        nearest_two = np.argpartition(-similarity, kth=1, axis=1)[:, :2]
        nearest_values = np.take_along_axis(similarity, nearest_two, axis=1)
        order = np.argsort(-nearest_values, axis=1)
        nearest_two = np.take_along_axis(nearest_two, order, axis=1)
        nearest_values = np.take_along_axis(nearest_values, order, axis=1)
        best_map[start:end] = nearest_two[:, 0]
        best_similarity[start:end] = nearest_values[:, 0]
        second_similarity[start:end] = nearest_values[:, 1]

        block_best_local = np.argmax(similarity, axis=0)
        block_best_similarity = similarity[block_best_local, map_indices]
        update = block_best_similarity > mutual_best_similarity
        mutual_best_similarity[update] = block_best_similarity[update]
        mutual_best_query[update] = start + block_best_local[update]

    best_distance_sq = np.maximum(0.0, 2.0 - 2.0 * best_similarity)
    second_distance_sq = np.maximum(1e-12, 2.0 - 2.0 * second_similarity)
    ratio_ok = best_distance_sq < float(ratio_test) ** 2 * second_distance_sq
    query_indices = np.arange(len(query), dtype=np.int64)
    keep = (
        ratio_ok
        & (best_similarity >= float(min_similarity))
        & (mutual_best_query[best_map] == query_indices)
    )
    return query_indices[keep], best_map[keep], best_similarity[keep]


def localize_features(
    query_keypoints: np.ndarray,
    query_descriptors: np.ndarray,
    map_points: np.ndarray,
    map_descriptors: np.ndarray,
    camera_matrix: np.ndarray,
    odom_to_camera: np.ndarray,
    image_shape: tuple[int, int],
    config: GlobalLocalizationConfig,
    *,
    map_descriptors_normalized: bool = False,
) -> tuple[Optional[LocalizationCandidate], dict[str, object]]:
    """Match a query image to the 3D descriptor map and solve global PnP."""

    match_started = time.perf_counter()
    query_indices, map_indices, similarities = match_descriptors(
        query_descriptors,
        map_descriptors,
        ratio_test=config.ratio_test,
        min_similarity=config.min_similarity,
        map_descriptors_normalized=map_descriptors_normalized,
    )
    descriptor_match_ms = (time.perf_counter() - match_started) * 1000.0
    diagnostics: dict[str, object] = {
        "query_features": int(len(query_keypoints)),
        "map_features": int(len(map_points)),
        "descriptor_matches": int(len(query_indices)),
        "descriptor_match_ms": round(descriptor_match_ms, 2),
        "median_similarity": (
            round(float(np.median(similarities)), 4) if len(similarities) else None
        ),
        "inliers": 0,
        "inlier_ratio": 0.0,
        "median_reprojection_error_px": None,
        "grid_cells": 0,
        "accepted": False,
        "rejection": None,
    }
    if len(query_indices) < config.min_matches:
        diagnostics["rejection"] = "insufficient_descriptor_matches"
        return None, diagnostics

    image_points = np.asarray(query_keypoints, dtype=np.float32)[query_indices]
    object_points = np.asarray(map_points, dtype=np.float32)[map_indices]
    return localize_correspondences(
        image_points,
        object_points,
        camera_matrix,
        odom_to_camera,
        image_shape,
        config,
        diagnostics=diagnostics,
    )


def localize_correspondences(
    image_points: np.ndarray,
    object_points: np.ndarray,
    camera_matrix: np.ndarray,
    odom_to_camera: np.ndarray,
    image_shape: tuple[int, int],
    config: GlobalLocalizationConfig,
    *,
    diagnostics: Optional[dict[str, object]] = None,
) -> tuple[Optional[LocalizationCandidate], dict[str, object]]:
    """Geometrically validate known 2D-to-3D correspondences with PnP."""

    image_points = np.asarray(image_points, dtype=np.float32).reshape(-1, 2)
    object_points = np.asarray(object_points, dtype=np.float32).reshape(-1, 3)
    if len(image_points) != len(object_points):
        raise ValueError("image and object points must have matching rows")
    if diagnostics is None:
        diagnostics = {
            "query_features": int(len(image_points)),
            "map_features": int(len(object_points)),
            "descriptor_matches": int(len(image_points)),
            "descriptor_match_ms": 0.0,
            "median_similarity": None,
            "inliers": 0,
            "inlier_ratio": 0.0,
            "median_reprojection_error_px": None,
            "grid_cells": 0,
            "accepted": False,
            "rejection": None,
        }
    if len(image_points) < config.min_matches:
        diagnostics["rejection"] = "insufficient_correspondences"
        return None, diagnostics

    ok, rvec, tvec, inlier_payload = cv2.solvePnPRansac(
        object_points,
        image_points,
        np.asarray(camera_matrix, dtype=np.float64).reshape(3, 3),
        None,
        flags=cv2.SOLVEPNP_EPNP,
        iterationsCount=1000,
        reprojectionError=float(config.max_reprojection_error_px),
        confidence=0.999,
    )
    if not ok or inlier_payload is None:
        diagnostics["rejection"] = "pnp_failed"
        return None, diagnostics
    inlier_indices = inlier_payload.reshape(-1)
    inlier_count = int(len(inlier_indices))
    inlier_ratio = inlier_count / float(len(image_points))
    diagnostics["inliers"] = inlier_count
    diagnostics["inlier_ratio"] = round(inlier_ratio, 4)
    if inlier_count < config.min_inliers:
        diagnostics["rejection"] = "insufficient_pnp_inliers"
        return None, diagnostics
    if inlier_ratio < config.min_inlier_ratio:
        diagnostics["rejection"] = "low_pnp_inlier_ratio"
        return None, diagnostics

    rvec, tvec = cv2.solvePnPRefineLM(
        object_points[inlier_indices],
        image_points[inlier_indices],
        np.asarray(camera_matrix, dtype=np.float64).reshape(3, 3),
        None,
        rvec,
        tvec,
    )
    projected, _ = cv2.projectPoints(
        object_points[inlier_indices],
        rvec,
        tvec,
        np.asarray(camera_matrix, dtype=np.float64).reshape(3, 3),
        None,
    )
    errors = np.linalg.norm(
        projected.reshape(-1, 2) - image_points[inlier_indices], axis=1
    )
    median_error = float(np.median(errors))
    diagnostics["median_reprojection_error_px"] = round(median_error, 3)
    if median_error > config.max_reprojection_error_px:
        diagnostics["rejection"] = "high_reprojection_error"
        return None, diagnostics

    height, width = image_shape
    inlier_pixels = image_points[inlier_indices]
    grid_x = np.clip((inlier_pixels[:, 0] * 4 / width).astype(int), 0, 3)
    grid_y = np.clip((inlier_pixels[:, 1] * 4 / height).astype(int), 0, 3)
    grid_cells = len(set(zip(grid_x.tolist(), grid_y.tolist())))
    diagnostics["grid_cells"] = grid_cells
    if grid_cells < config.min_grid_cells:
        diagnostics["rejection"] = "insufficient_spatial_coverage"
        return None, diagnostics

    rotation, _ = cv2.Rodrigues(rvec)
    camera_from_map = np.eye(4, dtype=np.float64)
    camera_from_map[:3, :3] = rotation
    camera_from_map[:3, 3] = tvec.reshape(3)
    map_to_camera = np.linalg.inv(camera_from_map)
    map_to_odom = map_to_camera @ np.linalg.inv(
        np.asarray(odom_to_camera, dtype=np.float64).reshape(4, 4)
    )
    diagnostics["accepted"] = True
    diagnostics["rejection"] = None
    return LocalizationCandidate(
        map_to_camera=map_to_camera,
        map_to_odom=map_to_odom,
        matches=len(image_points),
        inliers=inlier_count,
        inlier_ratio=inlier_ratio,
        median_reprojection_error_px=median_error,
        grid_cells=grid_cells,
    ), diagnostics


def associate_reference_points(
    query_points: np.ndarray,
    matched_reference_points: np.ndarray,
    reference_points: np.ndarray,
    reference_object_points: np.ndarray,
    *,
    max_distance_px: float = 0.75,
) -> tuple[np.ndarray, np.ndarray]:
    """Map direct image matches back to a reference keyframe's 3D points."""

    query = np.asarray(query_points, dtype=np.float32).reshape(-1, 2)
    matched_reference = np.asarray(
        matched_reference_points, dtype=np.float32
    ).reshape(-1, 2)
    reference = np.asarray(reference_points, dtype=np.float32).reshape(-1, 2)
    objects = np.asarray(reference_object_points, dtype=np.float32).reshape(-1, 3)
    if len(query) != len(matched_reference):
        raise ValueError("direct match point arrays must have matching rows")
    if len(reference) != len(objects):
        raise ValueError("reference image and object points must have matching rows")
    if max_distance_px <= 0.0:
        raise ValueError("reference association distance must be positive")
    if not len(query) or not len(reference):
        return (
            np.empty((0, 2), dtype=np.float32),
            np.empty((0, 3), dtype=np.float32),
        )

    distances_sq = np.sum(
        (matched_reference[:, None, :] - reference[None, :, :]) ** 2,
        axis=2,
    )
    nearest = np.argmin(distances_sq, axis=1)
    nearest_distance_sq = distances_sq[np.arange(len(query)), nearest]
    within = nearest_distance_sq <= float(max_distance_px) ** 2

    # SuperGlue normally returns unique reference keypoints. Retain only the
    # closest match if numerical association maps two results onto one point.
    selected: dict[int, int] = {}
    for match_index in np.flatnonzero(within):
        reference_index = int(nearest[match_index])
        previous = selected.get(reference_index)
        if (
            previous is None
            or nearest_distance_sq[match_index] < nearest_distance_sq[previous]
        ):
            selected[reference_index] = int(match_index)
    match_indices = np.asarray(sorted(selected.values()), dtype=np.int64)
    if not len(match_indices):
        return (
            np.empty((0, 2), dtype=np.float32),
            np.empty((0, 3), dtype=np.float32),
        )
    return query[match_indices], objects[nearest[match_indices]]


class LocalizationConsensus:
    """Accept a correction from a consistent quorum in a bounded attempt window."""

    def __init__(self, config: GlobalLocalizationConfig) -> None:
        self.config = config
        self.correction: Optional[np.ndarray] = None
        if config.confirmation_frames <= 0:
            raise ValueError("confirmation frames must be positive")
        if config.confirmation_window < config.confirmation_frames:
            raise ValueError("confirmation window must cover required frames")
        self._window: deque[Optional[LocalizationCandidate]] = deque(
            maxlen=config.confirmation_window
        )

    def observe(self, candidate: Optional[LocalizationCandidate]) -> dict[str, object]:
        self._window.append(candidate)
        available = [item for item in self._window if item is not None]
        best_cluster: list[LocalizationCandidate] = []
        for reference in available:
            cluster = []
            for item in available:
                translation = float(
                    np.linalg.norm(
                        reference.map_to_odom[:3, 3] - item.map_to_odom[:3, 3]
                    )
                )
                rotation = rotation_distance_deg(
                    reference.map_to_odom, item.map_to_odom
                )
                if (
                    translation <= self.config.confirmation_translation_m
                    and rotation <= self.config.confirmation_rotation_deg
                ):
                    cluster.append(item)
            if len(cluster) >= len(best_cluster):
                best_cluster = cluster
        progress = len(best_cluster)
        if progress >= self.config.confirmation_frames:
            translations = np.asarray(
                [item.map_to_odom[:3, 3] for item in best_cluster]
            )
            accepted = best_cluster[-1].map_to_odom.copy()
            accepted[:3, 3] = np.median(translations, axis=0)
            self.correction = accepted
            self._window.clear()
            progress = 0
        return {
            "localized": self.correction is not None,
            "confirmation_progress": progress,
            "confirmation_required": self.config.confirmation_frames,
            "confirmation_window": self.config.confirmation_window,
        }
