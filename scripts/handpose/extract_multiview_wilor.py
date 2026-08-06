#!/usr/bin/env python3
"""Extract WiLoR hands from three cameras and verify them in a shared map frame."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from dataclasses import dataclass
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import time
from typing import Optional

import cv2
import numpy as np
import torch
from rosbags.highlevel import AnyReader
from rosbags.typesys import Stores, get_typestore
from wilor_mini.pipelines.wilor_hand_pose3d_estimation_pipeline import (
    WiLorHandPose3dEstimationPipeline,
)

from .multiview import (
    HAND_CONNECTIONS,
    KEYPOINT_NAMES,
    MANO_JOINT_ROTATION_NAMES,
    center_to_left_from_baseline,
    interpolate_pose,
    json_ready,
    matrix_from_pose,
    nearest_record,
    numeric_summary,
    pose_from_matrix,
    project_points,
    quaternion_from_rotation,
    suppress_overlapping_hands,
    transform_points,
)


DEFAULT_MODEL_DIR = Path("/opt/insight/models/wilor")
MANO_SHAPE_NOTICE = (
    "WARNING: You are using a MANO model, with only 10 shape coefficients."
)
MAP_FRAME = "insight9_map"


@dataclass(frozen=True)
class CameraSpec:
    name: str
    role: str
    image_topic: str
    camera_info_topic: str
    pose_topic: str
    left_frame: str
    image_frame: str
    right_frame: str


CAMERAS = (
    CameraSpec(
        name="insight3_a",
        role="right_wrist",
        image_topic="/insight3_a/camera/infra1/image_rect_raw",
        camera_info_topic="/insight3_a/camera/infra1/camera_info",
        pose_topic="/insight_global/insight3_a/pose",
        left_frame="insight3_a_camera_left",
        image_frame="insight3_a_camera_left",
        right_frame="insight3_a_camera_right",
    ),
    CameraSpec(
        name="insight3_b",
        role="left_wrist",
        image_topic="/insight3_b/camera/infra1/image_rect_raw",
        camera_info_topic="/insight3_b/camera/infra1/camera_info",
        pose_topic="/insight_global/insight3_b/pose",
        left_frame="insight3_b_camera_left",
        image_frame="insight3_b_camera_left",
        right_frame="insight3_b_camera_right",
    ),
    CameraSpec(
        name="insight9_a",
        role="head",
        image_topic="/insight9_a/camera/color/image_rect_raw/compressed",
        camera_info_topic="/insight9_a/camera/color/camera_info",
        pose_topic="/insight9_sparse_map/pose",
        left_frame="insight9_a_camera_left",
        image_frame="insight9_a_camera_rgb",
        right_frame="insight9_a_camera_right",
    ),
)


class _KnownNoticeFilter:
    def __init__(self, stream) -> None:
        self.stream = stream

    def write(self, text: str) -> int:
        if MANO_SHAPE_NOTICE in text:
            return len(text)
        return self.stream.write(text)

    def flush(self) -> None:
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


class _CachedDetector:
    """Return one already-computed Ultralytics result to WiLoR predict()."""

    def __init__(self, result) -> None:
        self.result = result

    def __call__(self, *_args, **_kwargs):
        return [self.result]


def _stamp_ns(header) -> int:
    return int(header.stamp.sec) * 1_000_000_000 + int(header.stamp.nanosec)


def _message_transform(transform) -> np.ndarray:
    value = transform.transform
    return matrix_from_pose(
        (value.translation.x, value.translation.y, value.translation.z),
        (value.rotation.x, value.rotation.y, value.rotation.z, value.rotation.w),
    )


def _decode_image(message, msgtype: str) -> Optional[np.ndarray]:
    raw = np.frombuffer(message.data, dtype=np.uint8)
    if msgtype.endswith("/CompressedImage"):
        return cv2.imdecode(raw, cv2.IMREAD_COLOR)

    width = int(message.width)
    height = int(message.height)
    step = int(message.step)
    encoding = str(message.encoding).lower()
    if encoding in {"mono8", "8uc1", "nv12"}:
        required = height * step
        if step < width or raw.size < required:
            return None
        gray = raw[:required].reshape(height, step)[:, :width]
        return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    if encoding in {"rgb8", "bgr8"}:
        required = height * step
        if step < width * 3 or raw.size < required:
            return None
        color = raw[:required].reshape(height, step)[:, : width * 3]
        color = color.reshape(height, width, 3)
        return cv2.cvtColor(color, cv2.COLOR_RGB2BGR) if encoding == "rgb8" else color.copy()
    return None


def _camera_info_payload(message) -> dict:
    intrinsic = np.asarray(message.k, dtype=np.float64).reshape(3, 3)
    return {
        "frame_id": str(message.header.frame_id),
        "width": int(message.width),
        "height": int(message.height),
        "distortion_model": str(message.distortion_model),
        "distortion": [round(float(value), 9) for value in message.d],
        "intrinsic": [round(float(value), 9) for value in intrinsic.flatten()],
        "projection": [round(float(value), 9) for value in message.p],
    }


def _load_geometry(bag_dir: Path) -> tuple[dict, dict, dict]:
    typestore = get_typestore(Stores.ROS2_HUMBLE)
    info_by_name = {}
    pose_by_name = {camera.name: [] for camera in CAMERAS}
    static_transforms = {}
    topics = {
        *(camera.camera_info_topic for camera in CAMERAS),
        *(camera.pose_topic for camera in CAMERAS),
        "/tf_static",
    }
    info_topics = {camera.camera_info_topic: camera for camera in CAMERAS}
    pose_topics = {camera.pose_topic: camera for camera in CAMERAS}
    pose_frames = {camera.name: set() for camera in CAMERAS}
    with AnyReader([bag_dir], default_typestore=typestore) as reader:
        connections = [c for c in reader.connections if c.topic in topics]
        for connection, _bag_stamp, rawdata in reader.messages(connections=connections):
            message = reader.deserialize(rawdata, connection.msgtype)
            if connection.topic in info_topics:
                camera = info_topics[connection.topic]
                info_by_name.setdefault(camera.name, _camera_info_payload(message))
            elif connection.topic in pose_topics:
                camera = pose_topics[connection.topic]
                pose_frames[camera.name].add(str(message.header.frame_id))
                pose_by_name[camera.name].append(
                    (
                        _stamp_ns(message.header),
                        np.asarray(
                            [
                                message.pose.position.x,
                                message.pose.position.y,
                                message.pose.position.z,
                            ],
                            dtype=np.float64,
                        ),
                        np.asarray(
                            [
                                message.pose.orientation.x,
                                message.pose.orientation.y,
                                message.pose.orientation.z,
                                message.pose.orientation.w,
                            ],
                            dtype=np.float64,
                        ),
                    )
                )
            elif connection.topic == "/tf_static":
                for transform in message.transforms:
                    key = (str(transform.header.frame_id), str(transform.child_frame_id))
                    static_transforms[key] = _message_transform(transform)

    missing_info = [camera.name for camera in CAMERAS if camera.name not in info_by_name]
    missing_pose = [camera.name for camera in CAMERAS if not pose_by_name[camera.name]]
    if missing_info or missing_pose:
        raise RuntimeError(
            f"missing camera info {missing_info} or global pose {missing_pose}"
        )
    for camera in CAMERAS:
        if pose_frames[camera.name] != {MAP_FRAME}:
            raise RuntimeError(
                f"{camera.name} pose frames are {sorted(pose_frames[camera.name])}, expected {MAP_FRAME}"
            )
        pose_by_name[camera.name].sort(key=lambda sample: sample[0])
    return info_by_name, pose_by_name, static_transforms


def _center_to_image_transforms(static_transforms: dict) -> dict:
    results = {}
    for camera in CAMERAS:
        left_to_right_key = (camera.left_frame, camera.right_frame)
        if left_to_right_key not in static_transforms:
            raise RuntimeError(f"missing static transform {left_to_right_key}")
        center_to_left = center_to_left_from_baseline(
            static_transforms[left_to_right_key]
        )
        if camera.image_frame == camera.left_frame:
            results[camera.name] = center_to_left
            continue
        left_to_image_key = (camera.left_frame, camera.image_frame)
        if left_to_image_key not in static_transforms:
            raise RuntimeError(f"missing static transform {left_to_image_key}")
        results[camera.name] = center_to_left @ static_transforms[left_to_image_key]
    return results


def _predict_with_confidence(pipeline, frame: np.ndarray, confidence: float) -> list:
    detector = pipeline.hand_detector
    detections = detector(frame, conf=confidence, verbose=False)[0]
    scores = (
        detections.boxes.conf.detach().cpu().numpy().astype(np.float64).tolist()
        if detections.boxes is not None
        else []
    )
    pipeline.hand_detector = _CachedDetector(detections)
    try:
        outputs = pipeline.predict(frame, hand_conf=confidence)
    finally:
        pipeline.hand_detector = detector
    for index, output in enumerate(outputs):
        output["detector_confidence"] = float(scores[index]) if index < len(scores) else 0.0
    return outputs


def _round_array(values: np.ndarray, digits: int = 5) -> list:
    return [round(float(value), digits) for value in np.asarray(values).flatten()]


def _write_json(path: Path, payload: dict, *, compact: bool = False) -> None:
    with path.open("w", encoding="utf-8") as stream:
        json.dump(
            json_ready(payload),
            stream,
            ensure_ascii=False,
            allow_nan=False,
            **({"separators": (",", ":")} if compact else {"indent": 2}),
        )


def _draw_hand(frame: np.ndarray, hand: dict) -> None:
    points = np.asarray(hand["keypoints_image_px"], dtype=np.float64).reshape(21, 2)
    color = (255, 128, 0) if hand["handedness"] == "L" else (0, 128, 255)
    for first, second in HAND_CONNECTIONS:
        if np.all(np.isfinite(points[[first, second]])):
            cv2.line(
                frame,
                tuple(np.round(points[first]).astype(int)),
                tuple(np.round(points[second]).astype(int)),
                color,
                2,
                cv2.LINE_AA,
            )
    for point in points:
        if np.all(np.isfinite(point)):
            cv2.circle(frame, tuple(np.round(point).astype(int)), 3, (0, 0, 255), -1)
    x1, y1, x2, y2 = [int(round(value)) for value in hand["bbox_xyxy_px"]]
    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
    label = f"{hand['handedness']} {hand['confidence']:.2f}"
    cv2.putText(
        frame,
        label,
        (x1, max(25, y1 - 8)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        color,
        2,
        cv2.LINE_AA,
    )


def _video_writer(path: Path, width: int, height: int, fps: float):
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height)
    )
    if not writer.isOpened():
        raise RuntimeError(f"cannot create overlay video {path}")
    return writer


def _camera_quality(records: list[dict]) -> dict:
    hands = [hand for record in records for hand in record["hands"]]
    left_frames = sum(bool(record["hand_valid"]["left"]) for record in records)
    right_frames = sum(bool(record["hand_valid"]["right"]) for record in records)
    both_frames = sum(
        bool(record["hand_valid"]["left"] and record["hand_valid"]["right"])
        for record in records
    )
    any_frames = sum(bool(record["hands"]) for record in records)
    return {
        "total_frames": len(records),
        "image_decode_valid_frames": sum(record["image_valid"] for record in records),
        "pose_valid_frames": sum(record["pose_valid"] for record in records),
        "frames_with_any_hand": any_frames,
        "frames_with_left_hand": left_frames,
        "frames_with_right_hand": right_frames,
        "frames_with_both_hands": both_frames,
        "any_hand_detection_rate": round(any_frames / max(1, len(records)), 6),
        "both_hands_detection_rate": round(both_frames / max(1, len(records)), 6),
        "hand_detection_count": len(hands),
        "detector_confidence": numeric_summary([hand["confidence"] for hand in hands]),
        "wrist_depth_camera_m": numeric_summary(
            [hand["keypoints_camera_m"][2] for hand in hands]
        ),
        "wrist_quaternion_map_norm_abs_error": numeric_summary(
            [
                abs(
                    float(np.linalg.norm(hand["wrist_rotation_map_xyzw"]))
                    - 1.0
                )
                for hand in hands
            ]
        ),
        "nearest_pose_gap_ms": numeric_summary(
            [record["nearest_pose_gap_ms"] for record in records if record["pose_valid"]]
        ),
        "pose_bracket_gap_ms": numeric_summary(
            [record["pose_bracket_gap_ms"] for record in records if record["pose_valid"]]
        ),
        "duplicate_handedness_detections_discarded": sum(
            record["discarded_duplicate_handedness"] for record in records
        ),
        "overlapping_detections_discarded": sum(
            record["discarded_overlapping_detection"] for record in records
        ),
    }


def _record_transform(record: dict) -> np.ndarray:
    pose = record["image_pose_map"]
    return matrix_from_pose(pose["position_m"], pose["rotation_xyzw"])


def _hand_points(hand: dict, key: str) -> np.ndarray:
    width = 2 if key == "keypoints_image_px" else 3
    return np.asarray(hand[key], dtype=np.float64).reshape(21, width)


def _pair_hands(first: dict, second: dict) -> list[tuple[dict, dict]]:
    candidates = []
    for first_index, first_hand in enumerate(first["hands"]):
        first_wrist = _hand_points(first_hand, "keypoints_map_m")[0]
        for second_index, second_hand in enumerate(second["hands"]):
            second_wrist = _hand_points(second_hand, "keypoints_map_m")[0]
            distance = float(np.linalg.norm(first_wrist - second_wrist))
            candidates.append((distance, first_index, second_index))
    candidates.sort()
    matches = []
    used_first = set()
    used_second = set()
    for _distance, first_index, second_index in candidates:
        if first_index in used_first or second_index in used_second:
            continue
        used_first.add(first_index)
        used_second.add(second_index)
        matches.append((first["hands"][first_index], second["hands"][second_index]))
    return matches


def _reprojection_error(source_hand: dict, target_hand: dict, target_record: dict, intrinsic: np.ndarray) -> float:
    map_points = _hand_points(source_hand, "keypoints_map_m")
    target_to_map = _record_transform(target_record)
    camera_points = transform_points(np.linalg.inv(target_to_map), map_points)
    projected = project_points(camera_points, intrinsic)
    observed = _hand_points(target_hand, "keypoints_image_px")
    valid = np.all(np.isfinite(projected), axis=1) & np.all(np.isfinite(observed), axis=1)
    if not np.any(valid):
        return float("nan")
    return float(np.mean(np.linalg.norm(projected[valid] - observed[valid], axis=1)))


def _cross_view_quality(records_by_name: dict, info_by_name: dict, tolerance_ms: float) -> dict:
    camera_pairs = (
        ("insight3_a", "insight3_b"),
        ("insight3_a", "insight9_a"),
        ("insight3_b", "insight9_a"),
    )
    pair_results = {}
    for first_name, second_name in camera_pairs:
        second_records = records_by_name[second_name]
        intrinsic_first = np.asarray(info_by_name[first_name]["intrinsic"]).reshape(3, 3)
        intrinsic_second = np.asarray(info_by_name[second_name]["intrinsic"]).reshape(3, 3)
        measurements = []
        synchronized_frames = 0
        for first_record in records_by_name[first_name]:
            second_record = nearest_record(second_records, int(first_record["stamp_ns"]))
            if second_record is None:
                continue
            sync_delta_ms = abs(
                int(first_record["stamp_ns"]) - int(second_record["stamp_ns"])
            ) / 1e6
            if sync_delta_ms > tolerance_ms:
                continue
            synchronized_frames += 1
            if not first_record["pose_valid"] or not second_record["pose_valid"]:
                continue
            for first_hand, second_hand in _pair_hands(first_record, second_record):
                first_points = _hand_points(first_hand, "keypoints_map_m")
                second_points = _hand_points(second_hand, "keypoints_map_m")
                errors = np.linalg.norm(first_points - second_points, axis=1)
                reprojection_forward = _reprojection_error(
                    first_hand, second_hand, second_record, intrinsic_second
                )
                reprojection_reverse = _reprojection_error(
                    second_hand, first_hand, first_record, intrinsic_first
                )
                measurements.append(
                    {
                        "stamp_ns": int(first_record["stamp_ns"]),
                        "sync_delta_ms": round(sync_delta_ms, 6),
                        "first_handedness": first_hand["handedness"],
                        "second_handedness": second_hand["handedness"],
                        "handedness_agrees": first_hand["handedness"] == second_hand["handedness"],
                        "wrist_distance_m": round(float(errors[0]), 6),
                        "mpjpe_m": round(float(np.mean(errors)), 6),
                        "reprojection_first_to_second_px": round(reprojection_forward, 3),
                        "reprojection_second_to_first_px": round(reprojection_reverse, 3),
                        "symmetric_reprojection_px": round(
                            float(np.nanmean([reprojection_forward, reprojection_reverse])), 3
                        ),
                    }
                )
        confirmed = [
            item
            for item in measurements
            if item["mpjpe_m"] <= 0.15 and item["symmetric_reprojection_px"] <= 80.0
        ]
        key = f"{first_name}__{second_name}"
        pair_results[key] = {
            "synchronized_frame_pairs": synchronized_frames,
            "matched_hand_pairs": len(measurements),
            "handedness_agreement_rate": round(
                sum(item["handedness_agrees"] for item in measurements)
                / max(1, len(measurements)),
                6,
            ),
            "wrist_distance_m": numeric_summary(
                [item["wrist_distance_m"] for item in measurements]
            ),
            "mpjpe_m": numeric_summary([item["mpjpe_m"] for item in measurements]),
            "symmetric_reprojection_px": numeric_summary(
                [item["symmetric_reprojection_px"] for item in measurements]
            ),
            "heuristic_confirmed_pairs": len(confirmed),
            "heuristic_confirmation_rate": round(
                len(confirmed) / max(1, len(measurements)), 6
            ),
            "confirmation_thresholds": {
                "mpjpe_m_lte": 0.15,
                "symmetric_reprojection_px_lte": 80.0,
            },
            "measurements": measurements,
        }

    wrist_a = records_by_name["insight3_a"]
    wrist_b = records_by_name["insight3_b"]
    triple_synchronized = 0
    triple_with_any_detection = 0
    triple_with_same_handedness_detection = 0
    for head_record in records_by_name["insight9_a"]:
        first = nearest_record(wrist_a, int(head_record["stamp_ns"]))
        second = nearest_record(wrist_b, int(head_record["stamp_ns"]))
        if first is None or second is None:
            continue
        if max(
            abs(int(first["stamp_ns"]) - int(head_record["stamp_ns"])),
            abs(int(second["stamp_ns"]) - int(head_record["stamp_ns"])),
        ) / 1e6 > tolerance_ms:
            continue
        triple_synchronized += 1
        if first["hands"] and second["hands"] and head_record["hands"]:
            triple_with_any_detection += 1
        common = (
            {hand["handedness"] for hand in first["hands"]}
            & {hand["handedness"] for hand in second["hands"]}
            & {hand["handedness"] for hand in head_record["hands"]}
        )
        if common:
            triple_with_same_handedness_detection += 1
    return {
        "method": "nearest_timestamp_then_greedy_global_wrist_assignment",
        "sync_tolerance_ms": tolerance_ms,
        "coordinate_frame": MAP_FRAME,
        "is_ground_truth": False,
        "pairwise": pair_results,
        "triple": {
            "synchronized_frames": triple_synchronized,
            "frames_with_detection_in_all_views": triple_with_any_detection,
            "frames_with_same_handedness_in_all_views": triple_with_same_handedness_detection,
        },
    }


def _package_version() -> str:
    for distribution in ("wilor-mini", "wilor_mini"):
        try:
            return importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            continue
    return "unknown"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bag_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--hand-confidence", type=float, default=0.3)
    parser.add_argument("--max-frames-per-camera", type=int, default=0)
    parser.add_argument("--max-pose-bracket-gap-ms", type=float, default=100.0)
    parser.add_argument("--sync-tolerance-ms", type=float, default=25.0)
    parser.add_argument("--no-overlays", action="store_true")
    args = parser.parse_args()

    started = time.time()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    camera_dir = args.output_dir / "cameras"
    overlay_dir = args.output_dir / "overlays"
    camera_dir.mkdir(parents=True, exist_ok=True)
    if not args.no_overlays:
        overlay_dir.mkdir(parents=True, exist_ok=True)

    info_by_name, pose_by_name, static_transforms = _load_geometry(args.bag_dir)
    center_to_image = _center_to_image_transforms(static_transforms)
    print("MULTIVIEW_GEOMETRY_READY", flush=True)
    spec_by_topic = {camera.image_topic: camera for camera in CAMERAS}
    info_focal = {}
    for camera in CAMERAS:
        info = info_by_name[camera.name]
        intrinsic = np.asarray(info["intrinsic"], dtype=np.float64).reshape(3, 3)
        info_focal[camera.name] = (
            float(intrinsic[0, 0] + intrinsic[1, 1])
            * 0.5
            * 256.0
            / max(info["width"], info["height"])
        )

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    data_type = torch.float16 if device.type == "cuda" else torch.float32
    first_focal = info_focal[CAMERAS[0].name]
    print(f"MULTIVIEW_MODEL_LOADING {device.type} {data_type}", flush=True)
    with redirect_stdout(_KnownNoticeFilter(sys.stdout)):
        pipeline = WiLorHandPose3dEstimationPipeline(
            device=device,
            dtype=data_type,
            focal_length=first_focal,
            wilor_pretrained_dir=str(args.model_dir),
            verbose=False,
        )
    print("MULTIVIEW_MODEL_READY", flush=True)

    records_by_name = {camera.name: [] for camera in CAMERAS}
    processed_by_name = {camera.name: 0 for camera in CAMERAS}
    overlay_writers = {}
    total_expected = 0
    typestore = get_typestore(Stores.ROS2_HUMBLE)
    with AnyReader([args.bag_dir], default_typestore=typestore) as reader:
        connections = [
            connection
            for connection in reader.connections
            if connection.topic in spec_by_topic
        ]
        total_expected = sum(connection.msgcount for connection in connections)
        if args.max_frames_per_camera > 0:
            total_expected = min(
                total_expected, args.max_frames_per_camera * len(CAMERAS)
            )
        print(f"MULTIVIEW_START {total_expected}", flush=True)
        for connection, _bag_stamp, rawdata in reader.messages(connections=connections):
            camera = spec_by_topic[connection.topic]
            if (
                args.max_frames_per_camera > 0
                and processed_by_name[camera.name] >= args.max_frames_per_camera
            ):
                continue
            frame_index = processed_by_name[camera.name]
            processed_by_name[camera.name] += 1
            message = reader.deserialize(rawdata, connection.msgtype)
            stamp_ns = _stamp_ns(message.header)
            frame = _decode_image(message, connection.msgtype)
            pose_sample = interpolate_pose(
                pose_by_name[camera.name],
                stamp_ns,
                max_bracket_gap_ns=int(args.max_pose_bracket_gap_ms * 1e6),
            )
            pose_valid = pose_sample is not None
            image_to_map = None
            pose_payload = None
            if pose_sample is not None:
                image_to_map = pose_sample["transform"] @ center_to_image[camera.name]
                position, quaternion = pose_from_matrix(image_to_map)
                pose_payload = {
                    "position_m": _round_array(position, 6),
                    "rotation_xyzw": _round_array(quaternion, 8),
                }
            record = {
                "frame_index": frame_index,
                "stamp_ns": stamp_ns,
                "image_valid": frame is not None,
                "pose_valid": pose_valid,
                "nearest_pose_gap_ms": (
                    round(pose_sample["nearest_gap_ns"] / 1e6, 6)
                    if pose_sample is not None
                    else None
                ),
                "pose_bracket_gap_ms": (
                    round(pose_sample["bracket_gap_ns"] / 1e6, 6)
                    if pose_sample is not None
                    else None
                ),
                "image_pose_map": pose_payload,
                "hand_valid": {"left": False, "right": False},
                "hands": [],
                "discarded_duplicate_handedness": 0,
                "discarded_overlapping_detection": 0,
            }
            if frame is not None:
                focal = info_focal[camera.name]
                pipeline.FOCAL_LENGTH = focal
                pipeline.wilor_model.FOCAL_LENGTH = focal
                predictions = _predict_with_confidence(
                    pipeline, frame, args.hand_confidence
                )
                candidates = []
                for output in predictions:
                    prediction = output.get("wilor_preds")
                    if not prediction:
                        continue
                    camera_points = (
                        prediction["pred_keypoints_3d"][0]
                        + prediction["pred_cam_t_full"][0][None, :]
                    ).astype(np.float64)
                    image_points = prediction["pred_keypoints_2d"][0].astype(np.float64)
                    wrist_rotation_camera_rotvec = np.asarray(
                        prediction["global_orient"][0], dtype=np.float64
                    ).reshape(3)
                    mano_joint_rotation_rotvec = np.asarray(
                        prediction["hand_pose"][0], dtype=np.float64
                    ).reshape(15, 3)
                    wrist_rotation_camera, _jacobian = cv2.Rodrigues(
                        wrist_rotation_camera_rotvec
                    )
                    wrist_rotation_camera_xyzw = quaternion_from_rotation(
                        wrist_rotation_camera
                    )
                    wrist_rotation_map_xyzw = (
                        quaternion_from_rotation(
                            image_to_map[:3, :3] @ wrist_rotation_camera
                        )
                        if image_to_map is not None
                        else np.full(4, np.nan)
                    )
                    confidence = float(output["detector_confidence"])
                    handedness = "R" if bool(output["is_right"]) else "L"
                    finite = bool(
                        np.all(np.isfinite(camera_points))
                        and np.all(np.isfinite(image_points))
                        and np.all(np.isfinite(wrist_rotation_camera_rotvec))
                        and np.all(np.isfinite(mano_joint_rotation_rotvec))
                    )
                    depth_valid = bool(
                        finite and 0.05 <= float(camera_points[0, 2]) <= 2.0
                    )
                    map_points = (
                        transform_points(image_to_map, camera_points)
                        if image_to_map is not None and finite
                        else np.full((21, 3), np.nan)
                    )
                    candidates.append(
                        {
                            "handedness": handedness,
                            "valid": bool(finite and depth_valid and pose_valid),
                            "confidence": round(confidence, 6),
                            "confidence_type": "YOLO hand bounding-box confidence",
                            "bbox_xyxy_px": [
                                round(float(value), 3)
                                for value in output["hand_bbox"]
                            ],
                            "keypoints_image_px": _round_array(image_points, 3),
                            "keypoints_camera_m": _round_array(camera_points, 5),
                            "keypoints_map_m": _round_array(map_points, 5),
                            "wrist_rotation_camera_xyzw": _round_array(
                                wrist_rotation_camera_xyzw, 8
                            ),
                            "wrist_rotation_map_xyzw": _round_array(
                                wrist_rotation_map_xyzw, 8
                            ),
                            "mano_joint_rotation_axis_angle_rad": _round_array(
                                mano_joint_rotation_rotvec, 6
                            ),
                        }
                    )
                candidates, overlap_discarded = suppress_overlapping_hands(candidates)
                record["discarded_overlapping_detection"] = overlap_discarded
                used_handedness = set()
                for hand in candidates:
                    if hand["handedness"] in used_handedness:
                        record["discarded_duplicate_handedness"] += 1
                        continue
                    used_handedness.add(hand["handedness"])
                    record["hands"].append(hand)
                    record["hand_valid"][
                        "left" if hand["handedness"] == "L" else "right"
                    ] = bool(hand["valid"])
                if not args.no_overlays:
                    writer = overlay_writers.get(camera.name)
                    if writer is None:
                        writer = _video_writer(
                            overlay_dir / f"{camera.name}.mp4",
                            frame.shape[1],
                            frame.shape[0],
                            30.0,
                        )
                        overlay_writers[camera.name] = writer
                    for hand in record["hands"]:
                        _draw_hand(frame, hand)
                    cv2.putText(
                        frame,
                        f"{camera.name} frame={frame_index} pose={'ok' if pose_valid else 'missing'}",
                        (14, 30),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.65,
                        (0, 255, 0) if pose_valid else (0, 0, 255),
                        2,
                        cv2.LINE_AA,
                    )
                    writer.write(frame)
            records_by_name[camera.name].append(record)
            processed_total = sum(processed_by_name.values())
            if processed_total % 10 == 0 or processed_total == total_expected:
                detected_total = sum(
                    bool(item["hands"])
                    for records in records_by_name.values()
                    for item in records
                )
                print(
                    f"MULTIVIEW_PROGRESS {processed_total} {detected_total} {total_expected}",
                    flush=True,
                )
    for writer in overlay_writers.values():
        writer.release()

    camera_quality = {}
    for camera in CAMERAS:
        records = records_by_name[camera.name]
        camera_payload = {
            "camera": camera.name,
            "role": camera.role,
            "image_topic": camera.image_topic,
            "pose_topic": camera.pose_topic,
            "map_frame": MAP_FRAME,
            "image_optical_frame": camera.image_frame,
            "keypoint_coordinate_units": {
                "keypoints_image_px": "pixel",
                "keypoints_camera_m": "meter",
                "keypoints_map_m": "meter",
                "wrist_rotation_camera_xyzw": "unit quaternion xyzw",
                "wrist_rotation_map_xyzw": "unit quaternion xyzw",
                "mano_joint_rotation_axis_angle_rad": "axis-angle rotation vector, radian",
            },
            "records": records,
        }
        _write_json(camera_dir / f"{camera.name}.json", camera_payload, compact=True)
        camera_quality[camera.name] = _camera_quality(records)

    cross_view = _cross_view_quality(
        records_by_name, info_by_name, args.sync_tolerance_ms
    )
    quality = {
        "method": "WiLoR multi-view offline pseudo-label QC",
        "camera": camera_quality,
        "cross_view": cross_view,
        "limitations": [
            "Cross-view consistency is not ground-truth MPJPE accuracy.",
            "WiLoR monocular depth can have view-dependent scale bias.",
            "Insight3 wrist inputs are infrared luminance replicated to three channels.",
            "No temporal smoothing is applied; raw per-frame estimates are preserved.",
        ],
    }
    _write_json(args.output_dir / "quality.json", quality)

    camera_manifest = {}
    for camera in CAMERAS:
        info = info_by_name[camera.name]
        extrinsic_position, extrinsic_quaternion = pose_from_matrix(
            center_to_image[camera.name]
        )
        camera_manifest[camera.name] = {
            "role": camera.role,
            "image_topic": camera.image_topic,
            "pose_topic": camera.pose_topic,
            "image_optical_frame": camera.image_frame,
            "camera_center_pose_frame": f"{camera.name}_global_camera_center",
            "camera_info": info,
            "T_camera_center_image_optical": {
                "position_m": _round_array(extrinsic_position, 9),
                "rotation_xyzw": _round_array(extrinsic_quaternion, 9),
            },
            "wilor_focal_length_at_256px": round(info_focal[camera.name], 9),
            "result": f"cameras/{camera.name}.json",
            "overlay": (
                f"overlays/{camera.name}.mp4" if not args.no_overlays else None
            ),
        }
    manifest = {
        "schema_version": 1,
        "source_category": "B_offline_pseudo_label",
        "source_bag": args.bag_dir.name,
        "created_at_epoch_s": time.time(),
        "processing_seconds": round(time.time() - started, 3),
        "model": {
            "name": "WiLoR-mini",
            "package_version": _package_version(),
            "model_directory": str(args.model_dir),
            "hand_detector": "bundled YOLO detector.pt",
            "confidence_definition": "YOLO hand bounding-box confidence in [0,1]",
            "keypoint_confidence_available": False,
            "keypoint_standard": "WiLoR MANO joints reordered to OpenPose 21-keypoint order",
            "keypoint_names": KEYPOINT_NAMES,
            "mano_joint_rotation_names": MANO_JOINT_ROTATION_NAMES,
            "temporal_smoothing": "none",
            "failure_handling": "image/pose validity plus per-frame left/right boolean masks; missing hands remain explicit",
            "licenses": [
                "WiLoR weights: CC-BY-NC-ND",
                "MANO model: separate non-commercial research license",
            ],
        },
        "coordinate_conventions": {
            "camera_points": "rectified image optical frame, meters, +X right, +Y down, +Z forward",
            "map_points": f"{MAP_FRAME}, meters",
            "pose": "T_map_camera_center, quaternion xyzw",
            "composition": "T_map_image = T_map_camera_center * T_camera_center_image",
            "camera_center_definition": "stereo midpoint aligned with the left optical frame",
        },
        "parameters": {
            "hand_confidence": args.hand_confidence,
            "max_pose_bracket_gap_ms": args.max_pose_bracket_gap_ms,
            "sync_tolerance_ms": args.sync_tolerance_ms,
            "max_frames_per_camera": args.max_frames_per_camera,
        },
        "cameras": camera_manifest,
        "quality": "quality.json",
    }
    _write_json(args.output_dir / "manifest.json", manifest)
    print(
        "MULTIVIEW_DONE "
        f"{sum(processed_by_name.values())} {args.output_dir} {time.time() - started:.3f}",
        flush=True,
    )


if __name__ == "__main__":
    main()
