"""Bounded confidence retries using the same PnP and consensus quality gates."""

import numpy as np

from .global_localization import associate_reference_points, localize_correspondences


def localize_reference(matches, reference, camera_matrix, odom_to_camera,
                       image_shape, config, *, confidence, retry_confidence=None,
                       association_px=.75):
    """Retry weak correspondences once without repeating neural inference."""
    thresholds = [confidence]
    if retry_confidence is not None:
        if not 0 <= retry_confidence < confidence <= 1:
            raise ValueError("Retry confidence must be below the primary confidence")
        thresholds.append(retry_confidence)
    attempts = []
    candidate = None
    for threshold in thresholds:
        image_points, object_points = associate_reference_points(
            matches.left_points, matches.right_points, reference.pixels,
            reference.map_points, max_distance_px=association_px,
            match_scores=matches.scores, min_confidence=threshold)
        diagnostics = dict(
            query_features=matches.detected_left, map_features=len(reference.map_points),
            descriptor_matches=len(image_points), descriptor_match_ms=matches.backend_inference_ms,
            median_similarity=float(np.median(matches.scores)) if len(matches.scores) else None,
            inliers=0, inlier_ratio=0.0, median_reprojection_error_px=None,
            grid_cells=0, accepted=False, rejection=None,
            localization_method="superglue_keyframe",
            direct_superglue_matches=len(matches.left_points),
            direct_3d_matches=len(image_points),
            direct_confident_matches=int(np.count_nonzero(matches.scores >= threshold)),
            direct_confidence_used=float(threshold),
            calibration_keyframe_stamp_ns=reference.stamp_ns)
        candidate, diagnostics = localize_correspondences(
            image_points, object_points, camera_matrix, odom_to_camera,
            image_shape, config, diagnostics=diagnostics)
        attempts.append({key: diagnostics.get(key) for key in (
            "direct_confidence_used", "direct_3d_matches", "inliers",
            "inlier_ratio", "grid_cells", "accepted", "rejection")})
        if candidate is not None or diagnostics["rejection"] not in {
            "insufficient_correspondences", "insufficient_pnp_inliers",
            "insufficient_spatial_coverage",
        }:
            break
    diagnostics["direct_attempts"] = attempts
    return candidate, diagnostics
