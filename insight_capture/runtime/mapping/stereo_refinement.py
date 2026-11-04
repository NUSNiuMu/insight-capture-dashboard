"""Subpixel stereo alignment along the rectified epipolar line."""

from dataclasses import replace

import cv2
import numpy as np

from .superglue_backend import StereoMatches


def _align(source, target, point, seed):
    patch = cv2.getRectSubPix(source, (15, 15), tuple(map(float, point)))
    search = cv2.getRectSubPix(target, (23, 15), (float(seed[0]), float(point[1])))
    scores = cv2.matchTemplate(search, patch, cv2.TM_CCOEFF_NORMED).ravel()
    index = int(scores.argmax())
    if index == 0 or index == len(scores) - 1:
        return np.array([np.nan, np.nan], np.float32)
    a, b, c = map(float, scores[index - 1:index + 2])
    curvature = a - 2 * b + c
    if curvature >= -1e-6:
        return np.array([np.nan, np.nan], np.float32)
    offset = .5 * (a - c) / curvature
    return np.array([seed[0] + index - 4 + offset, point[1]], np.float32)


def refine_stereo_matches(left: np.ndarray, right: np.ndarray,
                          matches: StereoMatches) -> StereoMatches:
    """Refine SuperGlue seeds with the original displacement/round-trip bounds."""
    points = np.asarray(matches.left_points, np.float32)
    seeds = np.asarray(matches.right_points, np.float32)
    if not len(points):
        return matches
    pixels = np.array([_align(left, right, p, q) for p, q in zip(points, seeds)])
    valid = (np.isfinite(pixels).all(axis=1)
             & (np.linalg.norm(pixels - seeds, axis=1) <= 1.5)
             & (pixels[:, 0] >= 7) & (pixels[:, 0] < right.shape[1] - 7)
             & (pixels[:, 1] >= 7) & (pixels[:, 1] < right.shape[0] - 7))
    for index in np.flatnonzero(valid):
        back = _align(right, left, pixels[index], points[index])
        valid[index] = np.isfinite(back).all() and np.linalg.norm(back - points[index]) <= .5
    return replace(matches, left_points=points[valid], right_points=pixels[valid],
                   descriptors=matches.descriptors[valid], scores=matches.scores[valid])
