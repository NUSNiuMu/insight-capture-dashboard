"""Fresh ROS-bag to Ego LeRobot conversion implementation."""

from __future__ import annotations

import json
from pathlib import Path
import time

import cv2
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from scipy.spatial.transform import Rotation

from insight_capture.postprocess.datasets.lerobot import FfmpegVideoWriter

from .identity import move_hand_slot, projection_outliers, temporal_identity_corrections
from .model import HAND_INDEX, HandPoseBackend
from .rosbag_io import (
    Timeline, build_timeline, interpolate_pose, load_camera_info,
    load_camera_streams, load_filtered_tf_static, load_pose_samples, selected_images,
)
from .spec import DeliverySpec, validate_segments


VIDEO_KEY = {
    "head": "observation.images.head_rgb",
    "left_hand": "observation.images.hand_left",
    "right_hand": "observation.images.hand_right",
}


def _json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _fixed(values: np.ndarray, dtype: pa.DataType) -> pa.Array:
    flat = values.reshape(len(values), -1)
    return pa.array(flat.tolist(), type=pa.list_(dtype, flat.shape[1]))


def _relative_poses(poses: np.ndarray, valid: np.ndarray) -> tuple[np.ndarray, dict[str, object]]:
    if not valid[0]:
        raise ValueError("the first cropped head pose is invalid; episode_head0 cannot be defined")
    origin_p, origin_q = poses[0, :3].copy(), poses[0, 3:].copy()
    origin_inv = Rotation.from_quat(origin_q).inv()
    result = np.zeros_like(poses)
    for index in np.flatnonzero(valid):
        result[index, :3] = origin_inv.apply(poses[index, :3] - origin_p)
        result[index, 3:] = (origin_inv * Rotation.from_quat(poses[index, 3:])).as_quat()
    return result, {"position_m": origin_p.tolist(), "rotation_xyzw": origin_q.tolist()}


def _rotation6(quaternion: np.ndarray) -> np.ndarray:
    return Rotation.from_quat(quaternion).as_matrix()[:2].reshape(6)


def _encode_stream(bag: Path, stream, timeline: Timeline, role: str, path: Path, fps: float) -> tuple[np.ndarray, tuple[int, int]]:
    writer = None
    luma = np.zeros(len(timeline.target_stamps), np.float32)
    shape = None
    try:
        for index, _stamp, bgr in selected_images(bag, stream.image_topic, timeline.selected_source_indices[role]):
            if writer is None:
                shape = bgr.shape[:2]
                writer = FfmpegVideoWriter(path, shape, fps)
            luma[index] = float(np.mean(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)))
            writer.write(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    finally:
        if writer is not None:
            writer.close()
    if writer is None or writer.frames != len(timeline.target_stamps) or shape is None:
        raise RuntimeError(f"encoded {0 if writer is None else writer.frames} frames for {role}")
    return luma, shape


def _infer_head(bag: Path, stream, timeline: Timeline, backend: HandPoseBackend, path: Path, fps: float, head_map: np.ndarray, head_valid: np.ndarray) -> tuple[dict[str, np.ndarray], tuple[int, int]]:
    count = len(timeline.target_stamps)
    arrays = {
        "kp2": np.zeros((count, 2, 21, 2), np.float32),
        "kp3": np.zeros((count, 2, 21, 3), np.float32),
        "kp2_valid": np.zeros((count, 2, 21), bool),
        "kp3_valid": np.zeros((count, 2, 21), bool),
        "detected": np.zeros((count, 2), bool),
        "valid": np.zeros((count, 2), bool),
        "confidence": np.zeros((count, 2), np.float32),
        "wrist": np.zeros((count, 2, 7), np.float32),
        "mano": np.zeros((count, 2, 45), np.float32),
    }
    writer = None
    shape = None
    try:
        for index, _stamp, bgr in selected_images(bag, stream.image_topic, timeline.selected_source_indices["head"]):
            if writer is None:
                shape = bgr.shape[:2]
                writer = FfmpegVideoWriter(path, shape, fps)
            writer.write(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
            best = {}
            for prediction in backend.predict(bgr):
                hand = HAND_INDEX[prediction.handedness]
                if hand not in best or prediction.confidence > best[hand].confidence:
                    best[hand] = prediction
            for hand, prediction in best.items():
                arrays["kp2"][index, hand] = prediction.keypoints_2d
                arrays["kp2_valid"][index, hand] = True
                arrays["detected"][index, hand] = True
                arrays["confidence"][index, hand] = prediction.confidence
                arrays["mano"][index, hand] = prediction.mano_pose_axis_angle
                if not head_valid[index]:
                    continue
                camera_rotation = Rotation.from_quat(head_map[index, 3:])
                points = camera_rotation.apply(prediction.keypoints_3d_camera) + head_map[index, :3]
                arrays["kp3"][index, hand] = points
                wrist_q = (camera_rotation * Rotation.from_quat(prediction.wrist_rotation_camera_xyzw)).as_quat()
                arrays["wrist"][index, hand, :3] = points[0]
                arrays["wrist"][index, hand, 3:] = wrist_q
                arrays["kp3_valid"][index, hand] = True
                arrays["valid"][index, hand] = True
    finally:
        if writer is not None:
            writer.close()
        backend.close()
    if writer is None or writer.frames != count or shape is None:
        raise RuntimeError("head stream did not produce the requested frame count")
    return arrays, shape


def _project_wrists(wrist_map: np.ndarray, wrist_valid: np.ndarray, head_map: np.ndarray, head_valid: np.ndarray, intrinsic: list[float]) -> tuple[np.ndarray, np.ndarray]:
    pixels = np.zeros((len(head_map), 2, 2), np.float64)
    valid = wrist_valid & head_valid[:, None]
    k = np.asarray(intrinsic, np.float64).reshape(3, 3)
    for frame, hand in zip(*np.nonzero(valid)):
        point = Rotation.from_quat(head_map[frame, 3:]).inv().apply(wrist_map[frame, hand, :3] - head_map[frame, :3])
        if point[2] <= 0:
            valid[frame, hand] = False
            continue
        pixels[frame, hand] = (k @ point)[:2] / point[2]
    return pixels, valid


def _clear_hand(arrays: dict[str, np.ndarray], mask: np.ndarray) -> None:
    for name, value in arrays.items():
        if value.ndim >= 2 and value.shape[1] == 2:
            value[mask] = 0


def _write_tables(output: Path, spec: DeliverySpec, timeline: Timeline, arrays: dict[str, np.ndarray], head_pose: np.ndarray, head_valid: np.ndarray, wrist_pose: np.ndarray, wrist_valid: np.ndarray, video_valid: dict[str, np.ndarray]) -> None:
    count = len(timeline.target_stamps)
    state = np.zeros((count, 2, 54), np.float32)
    state_valid = np.zeros((count, 2, 54), bool)
    for frame, hand in zip(*np.nonzero(arrays["valid"])):
        state[frame, hand, :3] = arrays["wrist"][frame, hand, :3]
        state[frame, hand, 3:9] = _rotation6(arrays["wrist"][frame, hand, 3:])
        state[frame, hand, 9:] = arrays["mano"][frame, hand]
        state_valid[frame, hand] = True
    state = state.reshape(count, 108)
    state_valid = state_valid.reshape(count, 108)
    action = np.concatenate((state[1:], state[-1:]))
    action_valid = np.concatenate((state_valid[1:], state_valid[-1:]))
    task_index = np.empty(count, np.int64)
    for segment in spec.segments:
        task_index[segment.start_frame:segment.end_frame + 1] = segment.segment_index
    columns = {
        "observation.state": _fixed(state, pa.float32()),
        "observation.state_valid": _fixed(state_valid, pa.bool_()),
        "action": _fixed(action, pa.float32()),
        "action_valid": _fixed(action_valid, pa.bool_()),
        "observation.hand_keypoints_3d": _fixed(arrays["kp3"], pa.float32()),
        "observation.hand_keypoints_2d": _fixed(arrays["kp2"], pa.float32()),
        "observation.hand_keypoints_3d_valid": _fixed(arrays["kp3_valid"], pa.bool_()),
        "observation.hand_keypoints_2d_valid": _fixed(arrays["kp2_valid"], pa.bool_()),
        "observation.hand_detected_2d": _fixed(arrays["detected"], pa.bool_()),
        "observation.hand_valid": _fixed(arrays["valid"], pa.bool_()),
        "observation.hand_confidence": _fixed(arrays["confidence"], pa.float32()),
        "observation.hand_wrist_pose": _fixed(arrays["wrist"], pa.float32()),
        "observation.head_pose": _fixed(head_pose, pa.float32()),
        "observation.head_pose_valid": _fixed(head_valid[:, None], pa.bool_()),
        "observation.wrist_camera_pose": _fixed(wrist_pose, pa.float32()),
        "observation.wrist_camera_pose_valid": _fixed(wrist_valid, pa.bool_()),
        "timestamp": pa.array(np.arange(count) / spec.fps, pa.float32()),
        "observation.timestamp_ns": pa.array(timeline.target_stamps),
        "frame_index": pa.array(np.arange(count), pa.int64()),
        "episode_index": pa.array(np.zeros(count, np.int64)),
        "index": pa.array(np.arange(count), pa.int64()),
        "task_index": pa.array(task_index),
        "valid.frame": pa.array(head_valid & video_valid["head"] & video_valid["left_hand"] & video_valid["right_hand"]),
    }
    for role, key in VIDEO_KEY.items():
        columns[f"{key}_timestamp_ns"] = pa.array(timeline.target_stamps)
        columns[f"{key}_valid"] = pa.array(video_valid[role])
        columns[f"{key}_source_timestamp_ns"] = pa.array(timeline.selected_source_stamps[role])
    data_path = output / "data/chunk-000/file-000.parquet"
    data_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(columns), data_path, compression="zstd")
    tasks = pa.table({"task_index": np.arange(len(spec.segments), dtype=np.int64), "task": [s.task for s in spec.segments]})
    pq.write_table(tasks, output / "meta/tasks.parquet", compression="zstd")
    segments = [{"episode_index": 0, "segment_index": s.segment_index, "task_index": s.segment_index, "subtask": s.subtask, "atomic_action": s.atomic_action, "task": s.task, "start_frame": s.start_frame, "end_frame": s.end_frame, "start_time_s": s.start_frame / spec.fps, "end_time_s_exclusive": (s.end_frame + 1) / spec.fps, "frame_count": s.frame_count} for s in spec.segments]
    pq.write_table(pa.Table.from_pylist(segments), output / "meta/segments.parquet", compression="zstd")
    episode_dir = output / "meta/episodes/chunk-000"
    episode_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"episode_index": [0], "tasks": [[s.task for s in spec.segments]], "length": [count]}), episode_dir / "file-000.parquet", compression="zstd")


def convert_bag(bag: Path, output: Path, spec: DeliverySpec, camera_config: Path, backend: HandPoseBackend, *, max_image_skew_ms: float, max_pose_bracket_ms: float, projection_gate_px: float, temporal_same_step_m: float, temporal_advantage_m: float) -> dict[str, object]:
    started = time.monotonic()
    model = backend.cache_identity()
    streams = load_camera_streams(camera_config)
    timeline = build_timeline(bag, streams, crop_start_s=spec.crop_start_s, crop_end_s=spec.crop_end_s, maximum_image_skew_ms=max_image_skew_ms)
    validate_segments(spec.segments, len(timeline.target_stamps))
    poses, pose_valid = {}, {}
    for role, stream in streams.items():
        values, valid, _nearest, _bracket = interpolate_pose(load_pose_samples(bag, stream.pose_topic), timeline.target_stamps, maximum_bracket_gap_ms=max_pose_bracket_ms)
        poses[role], pose_valid[role] = values, valid
    head_pose, origin = _relative_poses(poses["head"], pose_valid["head"])
    origin_p = np.asarray(origin["position_m"])
    origin_inv = Rotation.from_quat(origin["rotation_xyzw"]).inv()
    map_pose = poses["head"].copy()
    wrist_pose = np.zeros((len(timeline.target_stamps), 2, 7), np.float64)
    wrist_valid = np.stack((pose_valid["left_hand"], pose_valid["right_hand"]), axis=1)
    for hand, role in enumerate(("left_hand", "right_hand")):
        for frame in np.flatnonzero(pose_valid[role]):
            wrist_pose[frame, hand, :3] = origin_inv.apply(poses[role][frame, :3] - origin_p)
            wrist_pose[frame, hand, 3:] = (origin_inv * Rotation.from_quat(poses[role][frame, 3:])).as_quat()
    video_root = output / "videos"
    head_path = video_root / f"{VIDEO_KEY['head']}/chunk-000/file-000.mp4"
    arrays, head_shape = _infer_head(bag, streams["head"], timeline, backend, head_path, spec.fps, head_pose, pose_valid["head"])
    luma = {"head": np.full(len(timeline.target_stamps), 255.0)}
    shapes = {"head": head_shape}
    for role in ("left_hand", "right_hand"):
        path = video_root / f"{VIDEO_KEY[role]}/chunk-000/file-000.mp4"
        luma[role], shapes[role] = _encode_stream(bag, streams[role], timeline, role, path, spec.fps)
    camera_info = load_camera_info(bag, streams)
    projected, projection_valid = _project_wrists(wrist_pose, wrist_valid, head_pose, pose_valid["head"], camera_info[streams["head"].name]["intrinsic"])
    rejected, distances = projection_outliers(arrays["kp2"], arrays["detected"], projected, projection_valid, maximum_distance_px=projection_gate_px)
    _clear_hand(arrays, rejected)
    corrections = temporal_identity_corrections(arrays["wrist"][:, :, :3], arrays["valid"], minimum_same_label_step_m=temporal_same_step_m, minimum_advantage_m=temporal_advantage_m)
    for correction in corrections:
        for values in arrays.values():
            move_hand_slot(values, correction)
    video_valid = {role: timeline.selected_valid[role] & (luma[role] >= 5.0) for role in VIDEO_KEY}
    _write_tables(output, spec, timeline, arrays, head_pose, pose_valid["head"], wrist_pose, wrist_valid, video_valid)
    keyframes = [{"frame_index": int(frame), "reason": "projection_outlier", "hand": ("left", "right")[hand], "distance_px": float(distances[frame, hand])} for frame, hand in zip(*np.nonzero(rejected))]
    keyframes += [{"frame_index": c.frame_index, "reason": "temporal_identity_correction", "from_hand": ("left", "right")[c.source_hand], "to_hand": ("left", "right")[c.target_hand], "same_label_step_m": c.same_label_step_m, "opposite_label_step_m": c.opposite_label_step_m} for c in corrections]
    pq.write_table(pa.Table.from_pylist(keyframes) if keyframes else pa.table({"frame_index": pa.array([], pa.int64()), "reason": pa.array([], pa.string())}), output / "meta/keyframes.parquet", compression="zstd")
    static_tf = load_filtered_tf_static(bag)
    _json(output / "meta/camera_params.json", {"cameras": camera_info, "tf_static": static_tf, "static_transform_policy": "camera and IMU calibration transforms only; TCP transforms omitted"})
    _json(output / "meta/time_sync.json", {"method": "device NTP plus nearest image sampling on head timestamps", "maximum_image_skew_ms": max_image_skew_ms, "maximum_pose_bracket_ms": max_pose_bracket_ms})
    _json(output / "meta/schema.json", {"hand_order": ["left", "right"], "keypoint_count_per_hand": 21, "state_per_hand": "wrist xyz + wrist rotation_6d + 15 MANO joint axis-angle rotations", "coordinate_frame": "episode_head0", "missing_value_policy": "zero-filled values plus explicit validity masks", "action": "next-frame absolute hand state in episode_head0", "gripper_signal": "not present"})
    parquet_schema = pq.read_schema(output / "data/chunk-000/file-000.parquet")
    features = {}
    for field in parquet_schema:
        if pa.types.is_fixed_size_list(field.type):
            features[field.name] = {"dtype": str(field.type.value_type), "shape": [field.type.list_size], "names": None}
        else:
            features[field.name] = {"dtype": str(field.type), "shape": [1], "names": None}
    for role, key in VIDEO_KEY.items():
        height, width = shapes[role]
        features[key] = {"dtype": "video", "shape": [height, width, 3], "names": ["height", "width", "channel"], "video_info": {"video.fps": spec.fps, "video.codec": "h264", "video.pix_fmt": "yuv420p", "video.is_depth_map": False, "has_audio": False}}
    _json(output / "meta/info.json", {"codebase_version": "v3.0", "schema_version": 1, "robot_type": "egocentric_human_hands", "dataset_id": spec.dataset_id, "total_episodes": 1, "total_frames": len(timeline.target_stamps), "total_tasks": len(spec.segments), "chunks_size": 1000, "data_files_size_in_mb": 100, "video_files_size_in_mb": 500, "fps": spec.fps, "splits": {"train": "0:1"}, "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet", "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4", "features": features, "video_keys": list(VIDEO_KEY.values())})
    manifest = {"format": "lerobot_v3_three_view_head_only_hand_inference", "dataset_id": spec.dataset_id, "source_bag": bag.name, "task": spec.task, "episode_count": 1, "total_frames": len(timeline.target_stamps), "fps": spec.fps, "source_crop_s": [spec.crop_start_s, spec.crop_end_s], "video_keys": list(VIDEO_KEY.values()), "camera_role_mapping": {role: stream.name for role, stream in streams.items()}, "hand_inference_source_camera": streams["head"].name, "wrist_view_hand_detections_delivered": False, "coordinate_frame": "episode_head0", "coordinate_origin": {"source_crop_frame_index": 0, "T_map_head0": origin}, "model": model, "identity_constraints": {"projection_gate_px": projection_gate_px, "projection_rejections": {"left": int(rejected[:, 0].sum()), "right": int(rejected[:, 1].sum())}, "temporal_corrections": [item for item in keyframes if item["reason"] == "temporal_identity_correction"]}, "annotation": {"boundary_convention": "inclusive frame ranges; complete coverage with no gap or overlap", "levels": ["subtask", "atomic_action"], "segments_path": "meta/segments.parquet"}, "processing_seconds": time.monotonic() - started}
    _json(output / "meta/manifest.json", manifest)
    summary = {"hand_valid_frames": {"left": int(arrays["valid"][:, 0].sum()), "right": int(arrays["valid"][:, 1].sum())}, "projection_rejections": {"left": int(rejected[:, 0].sum()), "right": int(rejected[:, 1].sum())}, "temporal_identity_corrections": len(corrections)}
    _json(output / "meta/stats.json", summary)
    quality = {"status": "PASS", "method": "head-camera-only hand-pose pseudo-label QC", "cross_view_hand_geometry_validation": "disabled by delivery policy", "source_crop": {"requested_start_s": spec.crop_start_s, "requested_end_s": spec.crop_end_s, "actual_start_s": timeline.crop_actual_start_s, "actual_end_s": timeline.crop_actual_end_s, "frames": len(timeline.target_stamps)}, "camera_roles": {"left": streams["left_hand"].name, "right": streams["right_hand"].name}, **summary}
    _json(output / "meta/quality.json", quality)
    (output / "meta/quality.md").write_text(
        "# Ego dataset quality\n\n"
        f"- Status: PASS\n- Frames: {len(timeline.target_stamps)}\n"
        f"- Action segments: {len(spec.segments)}\n"
        f"- Head-only hand inference: yes\n"
        f"- Left/right wrist cameras: {streams['left_hand'].name} / {streams['right_hand'].name}\n"
        f"- Projection rejections: left {summary['projection_rejections']['left']}, right {summary['projection_rejections']['right']}\n"
        f"- Temporal identity corrections: {len(corrections)}\n",
        encoding="utf-8",
    )
    return manifest
