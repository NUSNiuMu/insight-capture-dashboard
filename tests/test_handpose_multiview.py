import math

import numpy as np

from handpose.multiview import (
    bbox_iou,
    center_to_left_from_baseline,
    interpolate_pose,
    json_ready,
    matrix_from_pose,
    pose_from_matrix,
    project_points,
    quaternion_slerp,
    suppress_overlapping_hands,
    transform_points,
)


def test_pose_matrix_round_trip() -> None:
    angle = math.radians(65.0)
    quaternion = np.asarray([0.0, math.sin(angle / 2), 0.0, math.cos(angle / 2)])
    transform = matrix_from_pose([1.0, -2.0, 0.5], quaternion)
    position_out, quaternion_out = pose_from_matrix(transform)
    assert np.allclose(position_out, [1.0, -2.0, 0.5])
    assert abs(float(np.dot(quaternion, quaternion_out))) > 1.0 - 1e-9


def test_slerp_and_pose_interpolation() -> None:
    first = np.asarray([0.0, 0.0, 0.0, 1.0])
    second = np.asarray([0.0, 0.0, 1.0, 0.0])
    midpoint = quaternion_slerp(first, second, 0.5)
    assert np.allclose(midpoint, [0.0, 0.0, 2**-0.5, 2**-0.5])
    samples = [
        (0, np.asarray([0.0, 0.0, 0.0]), first),
        (100, np.asarray([2.0, 0.0, 0.0]), second),
    ]
    result = interpolate_pose(samples, 50, max_bracket_gap_ns=100)
    assert result is not None
    position, quaternion = pose_from_matrix(result["transform"])
    assert np.allclose(position, [1.0, 0.0, 0.0])
    assert abs(float(np.dot(midpoint, quaternion))) > 1.0 - 1e-9


def test_interpolation_rejects_large_gap() -> None:
    quaternion = np.asarray([0.0, 0.0, 0.0, 1.0])
    samples = [
        (0, np.zeros(3), quaternion),
        (101, np.ones(3), quaternion),
    ]
    assert interpolate_pose(samples, 50, max_bracket_gap_ns=100) is None


def test_center_to_left_and_projection() -> None:
    left_to_right = np.eye(4)
    left_to_right[0, 3] = 0.1
    center_to_left = center_to_left_from_baseline(left_to_right)
    point_center = transform_points(center_to_left, np.asarray([[0.0, 0.0, 1.0]]))
    assert np.allclose(point_center, [[-0.05, 0.0, 1.0]])
    pixels = project_points(
        np.asarray([[0.1, -0.2, 1.0]]),
        np.asarray([[300.0, 0.0, 270.0], [0.0, 300.0, 320.0], [0.0, 0.0, 1.0]]),
    )
    assert np.allclose(pixels, [[300.0, 260.0]])


def test_cross_class_bbox_suppression_keeps_higher_confidence() -> None:
    candidates = [
        {"handedness": "L", "confidence": 0.4, "bbox_xyxy_px": [0, 0, 100, 100]},
        {"handedness": "R", "confidence": 0.8, "bbox_xyxy_px": [5, 5, 105, 105]},
        {"handedness": "L", "confidence": 0.6, "bbox_xyxy_px": [200, 0, 300, 100]},
    ]
    kept, discarded = suppress_overlapping_hands(candidates)
    assert discarded == 1
    assert [item["confidence"] for item in kept] == [0.8, 0.6]
    assert bbox_iou(candidates[0]["bbox_xyxy_px"], candidates[1]["bbox_xyxy_px"]) > 0.65


def test_json_ready_converts_nonfinite_values_to_null() -> None:
    value = json_ready({"values": [1.0, float("nan"), np.float32("inf")]})
    assert value == {"values": [1.0, None, None]}
