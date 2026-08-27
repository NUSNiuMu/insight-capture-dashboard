#!/usr/bin/env python3
"""Replay cube-marker localization offline without applying pose corrections."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sys

import cv2
import numpy as np
from scipy.spatial.transform import Rotation


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from insight_capture.postprocess.datasets.ego_lerobot.rosbag_io import (  # noqa: E402
    interpolate_pose,
    load_pose_samples,
)
from insight_capture.runtime.mapping.cube_markers import (  # noqa: E402
    MultiCubeMarkerEstimator,
    load_cube_marker_config,
)
from insight_capture.runtime.mapping.geometry import (  # noqa: E402
    left_to_stereo_center,
    matrix_from_transform,
    rotation_distance_deg,
)
from insight_capture.runtime.mapping.global_localization import (  # noqa: E402
    GlobalLocalizationConfig,
    LocalizationCandidate,
    LocalizationConsensus,
)


IMAGE_TOPIC = "/insight9_a/camera/color/image_rect_raw/compressed"
CAMERA_INFO_TOPIC = "/insight9_a/camera/color/camera_info"
HEAD_POSE_TOPIC = "/insight9_sparse_map/pose"


def _stamp_ns(message: object) -> int:
    stamp = message.header.stamp
    return int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)


def _pose_matrix(values: np.ndarray) -> np.ndarray:
    result = np.eye(4, dtype=np.float64)
    result[:3, :3] = Rotation.from_quat(values[3:]).as_matrix()
    result[:3, 3] = values[:3]
    return result


def _summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "median": None, "p95": None, "max": None}
    array = np.asarray(values, dtype=np.float64)
    return {
        "count": int(array.size),
        "median": round(float(np.median(array)), 6),
        "p95": round(float(np.percentile(array, 95)), 6),
        "max": round(float(np.max(array)), 6),
    }


def _axis_summary(values: np.ndarray) -> dict[str, list[float] | int]:
    if not len(values):
        return {"count": 0}
    array = np.asarray(values, dtype=np.float64).reshape(-1, 3)
    return {
        "count": int(len(array)),
        "median": np.round(np.median(array, axis=0), 6).tolist(),
        "p05": np.round(np.percentile(array, 5, axis=0), 6).tolist(),
        "p95": np.round(np.percentile(array, 95, axis=0), 6).tolist(),
        "min": np.round(np.min(array, axis=0), 6).tolist(),
        "max": np.round(np.max(array, axis=0), 6).tolist(),
        "std": np.round(np.std(array, axis=0), 6).tolist(),
        "positive_fraction": np.round(np.mean(array > 0.0, axis=0), 6).tolist(),
    }


def _load_reader(path: Path):
    from rosbags.highlevel import AnyReader
    from rosbags.typesys import Stores, get_typestore

    return AnyReader(
        [path], default_typestore=get_typestore(Stores.ROS2_HUMBLE)
    )


def _load_head_calibration(bag: Path) -> tuple[np.ndarray, np.ndarray]:
    camera_matrix = None
    left_to_right = None
    left_to_rgb = None
    with _load_reader(bag) as reader:
        connections = [
            item
            for item in reader.connections
            if item.topic in {CAMERA_INFO_TOPIC, "/tf_static"}
        ]
        for connection, _record_stamp, raw in reader.messages(connections=connections):
            message = reader.deserialize(raw, connection.msgtype)
            if connection.topic == CAMERA_INFO_TOPIC and camera_matrix is None:
                camera_matrix = np.asarray(message.k, dtype=np.float64).reshape(3, 3)
                continue
            if connection.topic != "/tf_static":
                continue
            for item in message.transforms:
                key = (str(item.header.frame_id), str(item.child_frame_id))
                value = item.transform
                transform = matrix_from_transform(
                    [value.translation.x, value.translation.y, value.translation.z],
                    [value.rotation.x, value.rotation.y, value.rotation.z, value.rotation.w],
                )
                if key == (
                    "insight9_a_camera_left",
                    "insight9_a_camera_right",
                ):
                    left_to_right = transform
                elif key == (
                    "insight9_a_camera_left",
                    "insight9_a_camera_rgb",
                ):
                    left_to_rgb = transform
    if camera_matrix is None or left_to_right is None or left_to_rgb is None:
        raise ValueError("bag is missing Insight9 CameraInfo or RGB static TF")
    center_to_rgb = np.linalg.inv(left_to_stereo_center(left_to_right)) @ left_to_rgb
    return camera_matrix, center_to_rgb


def replay(bag: Path, config_path: Path, camera: str) -> dict[str, object]:
    config = load_cube_marker_config(config_path)
    if not config.enabled:
        raise ValueError("cube marker localization is disabled")
    target = config.targets.get(camera)
    if target is None:
        raise ValueError(f"cube marker target is not configured for {camera}")

    camera_matrix, head_center_to_rgb = _load_head_calibration(bag)
    estimator = MultiCubeMarkerEstimator(config)
    dictionary_id = getattr(cv2.aruco, config.dictionary_name)
    detector = cv2.aruco.ArucoDetector(
        cv2.aruco.getPredefinedDictionary(dictionary_id),
        cv2.aruco.DetectorParameters(),
    )

    attempts: list[tuple[int, object | None, tuple[int, ...]]] = []
    period_ns = int(round(1e9 / config.detection_hz))
    next_stamp_ns = -1
    target_ids = set(target.marker_corners_cube_m)
    with _load_reader(bag) as reader:
        connections = [item for item in reader.connections if item.topic == IMAGE_TOPIC]
        if not connections:
            raise ValueError(f"bag is missing {IMAGE_TOPIC}")
        for connection, _record_stamp, raw in reader.messages(connections=connections):
            message = reader.deserialize(raw, connection.msgtype)
            stamp_ns = _stamp_ns(message)
            if next_stamp_ns < 0:
                next_stamp_ns = stamp_ns
            if stamp_ns < next_stamp_ns - period_ns // 4:
                continue
            while next_stamp_ns <= stamp_ns + period_ns // 4:
                next_stamp_ns += period_ns
            image = cv2.imdecode(
                np.frombuffer(message.data, dtype=np.uint8), cv2.IMREAD_GRAYSCALE
            )
            if image is None:
                attempts.append((stamp_ns, None, ()))
                continue
            corners, ids, _rejected = detector.detectMarkers(image)
            detections = {}
            if ids is not None:
                for points, marker_id in zip(corners, ids.reshape(-1)):
                    marker_id = int(marker_id)
                    if marker_id in target_ids:
                        detections[marker_id] = np.asarray(
                            points, dtype=np.float64
                        ).reshape(4, 2)
            estimate = estimator.estimate(detections, camera_matrix).get(camera)
            attempts.append((stamp_ns, estimate, tuple(sorted(detections))))

    stamps = np.asarray([item[0] for item in attempts], dtype=np.int64)
    head_pose, head_valid, _, _ = interpolate_pose(
        load_pose_samples(bag, HEAD_POSE_TOPIC),
        stamps,
        maximum_bracket_gap_ms=100.0,
    )
    hand_pose_topic = f"/insight_global/{camera}/pose"
    hand_pose, hand_valid, _, _ = interpolate_pose(
        load_pose_samples(bag, hand_pose_topic),
        stamps,
        maximum_bracket_gap_ms=100.0,
    )

    consensus = LocalizationConsensus(
        GlobalLocalizationConfig(
            min_matches=4,
            min_inliers=4,
            min_inlier_ratio=config.min_inlier_ratio,
            max_reprojection_error_px=config.max_reprojection_error_px,
            min_grid_cells=1,
            confirmation_frames=config.confirmation_frames,
            confirmation_window=config.confirmation_window,
            confirmation_translation_m=config.confirmation_translation_m,
            confirmation_rotation_deg=config.confirmation_rotation_deg,
        )
    )
    combinations: Counter[tuple[int, ...]] = Counter()
    accepted_combinations: Counter[tuple[int, ...]] = Counter()
    rejection_counts: Counter[str] = Counter()
    reprojection_errors: list[float] = []
    raw_translation_m: list[float] = []
    raw_rotation_deg: list[float] = []
    local_translation_xyz_m: list[np.ndarray] = []
    local_rotation_xyz_deg: list[np.ndarray] = []
    local_translation_by_markers: defaultdict[tuple[int, ...], list[np.ndarray]] = (
        defaultdict(list)
    )
    confirmation_events = []
    previous_correction = None
    start_stamp_ns = int(stamps[0])

    for index, (stamp_ns, estimate, combination) in enumerate(attempts):
        combinations[combination] += 1
        if not head_valid[index] or not hand_valid[index]:
            rejection_counts["missing_pose_bracket"] += 1
            consensus.observe(None)
            continue
        if estimate is None:
            rejection_counts[
                "marker_not_detected" if not combination else "pnp_quality_rejected"
            ] += 1
            consensus.observe(None)
            continue

        accepted_combinations[estimate.marker_ids] += 1
        reprojection_errors.append(estimate.median_reprojection_error_px)
        global_from_head = _pose_matrix(head_pose[index])
        global_from_rgb = global_from_head @ head_center_to_rgb
        global_from_recorded_hand = _pose_matrix(hand_pose[index])
        global_from_marker_hand = (
            global_from_rgb
            @ estimate.rgb_from_cube
            @ target.cube_from_camera_center
        )

        # This left-multiplying delta is the observable shadow correction from
        # the bag's published camera-center pose to the marker-predicted pose.
        correction_delta = (
            global_from_marker_hand @ np.linalg.inv(global_from_recorded_hand)
        )
        local_delta = (
            np.linalg.inv(global_from_recorded_hand) @ global_from_marker_hand
        )
        raw_translation_m.append(float(np.linalg.norm(local_delta[:3, 3])))
        raw_rotation_deg.append(rotation_distance_deg(np.eye(4), local_delta))
        local_translation_xyz_m.append(local_delta[:3, 3].copy())
        local_translation_by_markers[estimate.marker_ids].append(
            local_delta[:3, 3].copy()
        )
        local_rotation_xyz_deg.append(
            Rotation.from_matrix(local_delta[:3, :3]).as_euler("xyz", degrees=True)
        )

        candidate = LocalizationCandidate(
            map_to_camera=global_from_marker_hand,
            map_to_odom=correction_delta,
            matches=estimate.corners,
            inliers=estimate.inliers,
            inlier_ratio=estimate.inlier_ratio,
            median_reprojection_error_px=estimate.median_reprojection_error_px,
            grid_cells=len(estimate.marker_ids),
        )
        transition = consensus.observe(candidate)
        correction = consensus.correction
        changed = correction is not None and (
            previous_correction is None
            or not np.array_equal(correction, previous_correction)
        )
        if changed:
            previous_correction = correction.copy()
            confirmation_events.append(
                {
                    "time_s": round((stamp_ns - start_stamp_ns) / 1e9, 3),
                    "marker_ids": list(estimate.marker_ids),
                    "correction_mode": "shadow",
                    "shadow_translation_delta_m": round(
                        float(np.linalg.norm(correction[:3, 3])), 6
                    ),
                    "shadow_rotation_delta_deg": round(
                        rotation_distance_deg(np.eye(4), correction), 6
                    ),
                }
            )
        if not transition["localized"]:
            rejection_counts["awaiting_consensus"] += 1

    local_translation = np.asarray(local_translation_xyz_m, dtype=np.float64)
    local_rotation = np.asarray(local_rotation_xyz_deg, dtype=np.float64)
    confirmed_translation = [
        float(item["shadow_translation_delta_m"])
        for item in confirmation_events
    ]
    confirmed_rotation = [
        float(item["shadow_rotation_delta_deg"])
        for item in confirmation_events
    ]
    return {
        "bag": str(bag),
        "config": str(config_path),
        "camera": camera,
        "replay_mode": "shadow",
        "configured_apply_corrections": config.apply_corrections,
        "detection_hz": config.detection_hz,
        "attempts": len(attempts),
        "detected_combinations": {
            str(key): value for key, value in combinations.most_common()
        },
        "accepted_combinations": {
            str(key): value for key, value in accepted_combinations.most_common()
        },
        "rejections": dict(rejection_counts),
        "pnp_reprojection_median_px": _summary(reprojection_errors),
        "raw_shadow_translation_m": _summary(raw_translation_m),
        "raw_shadow_rotation_deg": _summary(raw_rotation_deg),
        "raw_shadow_local_translation_median_xyz": (
            np.round(np.median(local_translation, axis=0), 6).tolist()
            if len(local_translation)
            else None
        ),
        "raw_shadow_local_translation_xyz_distribution": _axis_summary(
            local_translation
        ),
        "raw_shadow_local_translation_by_marker_ids": {
            str(marker_ids): _axis_summary(np.asarray(values))
            for marker_ids, values in sorted(local_translation_by_markers.items())
        },
        "raw_shadow_local_rotation_median_xyz_deg": (
            np.round(np.median(local_rotation, axis=0), 6).tolist()
            if len(local_rotation)
            else None
        ),
        "confirmed_shadow_observations": len(confirmation_events),
        "confirmed_shadow_translation_m": _summary(confirmed_translation),
        "confirmed_shadow_rotation_deg": _summary(confirmed_rotation),
        "confirmation_events": confirmation_events,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bag", type=Path)
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "config/devices/jetson-nx/runtime.json",
    )
    parser.add_argument("--camera", default="insight3_a")
    args = parser.parse_args()
    print(
        json.dumps(
            replay(args.bag.resolve(), args.config.resolve(), args.camera),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
