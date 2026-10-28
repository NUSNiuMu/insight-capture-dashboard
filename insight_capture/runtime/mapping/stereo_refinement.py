"""Subpixel stereo alignment that preserves left-feature descriptor identities."""

from dataclasses import replace

import cv2
import numpy as np

from .superglue_backend import StereoMatches


def refine_stereo_matches(left: np.ndarray, right: np.ndarray,
                          matches: StereoMatches) -> StereoMatches:
    """Refine right pixels from SuperGlue seeds; discard failed round trips."""
    points = np.asarray(matches.left_points, dtype=np.float32).reshape(-1, 1, 2)
    target = np.asarray(matches.right_points, dtype=np.float32).reshape(-1, 1, 2)
    if not len(points):
        return matches
    options = dict(winSize=(15, 15), maxLevel=0,
                   criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, .01),
                   flags=cv2.OPTFLOW_USE_INITIAL_FLOW)
    refined, forward, _ = cv2.calcOpticalFlowPyrLK(
        left, right, points, target.copy(), **options)
    pixels = refined.reshape(-1, 2)
    valid = ((forward.ravel() != 0) & np.isfinite(pixels).all(axis=1)
             & (np.linalg.norm(pixels - target.reshape(-1, 2), axis=1) <= 1.5)
             & (pixels[:, 0] >= 7) & (pixels[:, 0] < right.shape[1] - 7)
             & (pixels[:, 1] >= 7) & (pixels[:, 1] < right.shape[0] - 7))
    indices = np.flatnonzero(valid)
    if len(indices):
        backward, reverse, _ = cv2.calcOpticalFlowPyrLK(
            right, left, refined[indices], points[indices].copy(), **options)
        back = backward.reshape(-1, 2)
        valid[indices] = ((reverse.ravel() != 0) & np.isfinite(back).all(axis=1)
                          & (np.linalg.norm(back - points[indices].reshape(-1, 2), axis=1) <= .5))
    # Keep the source pixel and descriptor together; only its right correspondence moves.
    return replace(matches, left_points=matches.left_points[valid],
                   right_points=pixels[valid], descriptors=matches.descriptors[valid],
                   scores=matches.scores[valid])
