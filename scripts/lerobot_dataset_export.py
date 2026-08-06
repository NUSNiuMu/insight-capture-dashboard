#!/usr/bin/env python3

"""Convert recorded ROS 2 bags into a LeRobot v3 dataset shaped like HiFi-UMI."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from scipy.spatial.transform import Rotation

import umi_dataset_export as umi
from hand_tracking.extract_gripper import decode_image


SCHEMA_VERSION = 2
ARM_DIM = 10
VIDEO_KEY_BY_ROLE = {
    "right_hand": "observation.images.right_wrist_0_rgb",
    "left_hand": "observation.images.left_wrist_0_rgb",
    "head": "observation.images.base_0_rgb",
}
DUAL_ARM_OFFSET = {"left_hand": 0, "right_hand": ARM_DIM}
HAND_LABEL = {"right_hand": "right", "left_hand": "left"}
ARM_STATE_NAMES = (
    "position.x",
    "position.y",
    "position.z",
    "rotation_6d.0",
    "rotation_6d.1",
    "rotation_6d.2",
    "rotation_6d.3",
    "rotation_6d.4",
    "rotation_6d.5",
    "gripper_width",
)
ROTATION_6D_PLACEHOLDER = np.asarray(
    [1.0, 0.0, 0.0, 0.0, 1.0, 0.0], dtype=np.float32
)


class FfmpegVideoWriter:
    """Stream RGB frames to an H.264 MP4 without retaining them in memory."""

    def __init__(self, path: Path, shape: tuple[int, int], fps: float) -> None:
        height, width = shape
        if height % 2 or width % 2:
            raise ValueError(
                f"H.264 yuv420p requires even image dimensions, got {width}x{height}"
            )
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise RuntimeError("ffmpeg is required for LeRobot MP4 export")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.shape = shape
        self.frames = 0
        self._process = subprocess.Popen(
            [
                ffmpeg,
                "-y",
                "-loglevel",
                "error",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-s:v",
                f"{width}x{height}",
                "-r",
                f"{fps:g}",
                "-i",
                "pipe:0",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "18",
                "-g",
                "2",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    def write(self, frame: np.ndarray) -> None:
        expected = (*self.shape, 3)
        if frame.shape != expected or frame.dtype != np.uint8:
            raise ValueError(
                f"video frame must be uint8 {expected}, got {frame.dtype} {frame.shape}"
            )
        assert self._process.stdin is not None
        try:
            self._process.stdin.write(np.ascontiguousarray(frame).tobytes())
        except BrokenPipeError as exc:
            raise RuntimeError(self._error_text() or "ffmpeg stopped early") from exc
        self.frames += 1

    def close(self) -> None:
        if self._process.stdin is not None and not self._process.stdin.closed:
            self._process.stdin.close()
        return_code = self._process.wait()
        if return_code:
            raise RuntimeError(self._error_text() or f"ffmpeg exited with {return_code}")

    def abort(self) -> None:
        if self._process.poll() is None:
            self._process.kill()
            self._process.wait()

    def _error_text(self) -> str:
        if self._process.stderr is None:
            return ""
        return self._process.stderr.read().decode("utf-8", errors="replace").strip()


def _json_write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _fixed_list(values: np.ndarray, value_type: pa.DataType) -> pa.Array:
    return pa.array(values.tolist(), type=pa.list_(value_type, values.shape[1]))


def _feature_stats(values: np.ndarray) -> dict[str, list[float] | list[int]]:
    values = np.asarray(values, dtype=np.float64)
    if values.ndim == 1:
        values = values[:, None]
    return {
        "min": values.min(axis=0).tolist(),
        "max": values.max(axis=0).tolist(),
        "mean": values.mean(axis=0).tolist(),
        "std": values.std(axis=0).tolist(),
        "count": [int(len(values))],
    }


def _hand_specs(specs: list[umi.CameraSpec]) -> list[umi.CameraSpec]:
    return [spec for spec in specs if spec.role != "head"]


def _state_schema(specs: list[umi.CameraSpec]) -> tuple[int, list[str]]:
    hands = _hand_specs(specs)
    if len(hands) == 1:
        return ARM_DIM, list(ARM_STATE_NAMES)
    if len(hands) == 2:
        return (
            2 * ARM_DIM,
            [
                f"{side}.{name}"
                for side in ("left", "right")
                for name in ARM_STATE_NAMES
            ],
        )
    raise ValueError(f"LeRobot export requires one or two arms, got {len(hands)}")


def _video_key(spec: umi.CameraSpec, specs: list[umi.CameraSpec]) -> str:
    if spec.role == "head":
        return VIDEO_KEY_BY_ROLE[spec.role]
    if len(_hand_specs(specs)) == 1:
        return "observation.images.right_wrist_0_rgb"
    return VIDEO_KEY_BY_ROLE[spec.role]


def _video_valid_key(video_key: str) -> str:
    return f"{video_key}_valid"


def _video_timestamp_key(video_key: str) -> str:
    return f"{video_key}_timestamp_ns"


def _episode_state(
    plan: umi.EpisodePlan, specs: list[umi.CameraSpec]
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    frame_count = len(plan.timestamps_ns)
    state_dim, _state_names = _state_schema(specs)
    state = np.zeros((frame_count, state_dim), dtype=np.float32)
    state_valid = np.zeros((frame_count, state_dim), dtype=bool)
    hands = _hand_specs(specs)
    for robot_index, spec in enumerate(hands):
        offset = 0 if len(hands) == 1 else DUAL_ARM_OFFSET[spec.role]
        positions = np.asarray(
            plan.lowdim[f"robot{robot_index}_eef_pos"], dtype=np.float32
        )
        rotvecs = np.asarray(
            plan.lowdim[f"robot{robot_index}_eef_rot_axis_angle"], dtype=np.float64
        )
        widths = np.asarray(
            plan.lowdim[f"robot{robot_index}_gripper_width"], dtype=np.float32
        ).reshape(frame_count, 1)

        position_valid = np.isfinite(positions)
        rotation_valid_rows = np.all(np.isfinite(rotvecs), axis=1)
        rotation_6d = np.repeat(
            ROTATION_6D_PLACEHOLDER[None, :], frame_count, axis=0
        )
        if np.any(rotation_valid_rows):
            converted = (
                Rotation.from_rotvec(rotvecs[rotation_valid_rows])
                .as_matrix()[:, :2, :]
                .reshape(-1, 6)
                .astype(np.float32)
            )
            converted_valid = np.all(np.isfinite(converted), axis=1)
            valid_indices = np.flatnonzero(rotation_valid_rows)
            rotation_6d[valid_indices[converted_valid]] = converted[converted_valid]
            rotation_valid_rows[valid_indices[~converted_valid]] = False
        rotation_valid = np.repeat(rotation_valid_rows[:, None], 6, axis=1)
        width_valid = np.isfinite(widths)

        arm_state = np.concatenate(
            (
                np.where(position_valid, positions, 0.0),
                rotation_6d,
                np.where(width_valid, widths, 0.0),
            ),
            axis=1,
        ).astype(np.float32)
        arm_valid = np.concatenate(
            (position_valid, rotation_valid, width_valid), axis=1
        )
        state[:, offset : offset + ARM_DIM] = arm_state
        state_valid[:, offset : offset + ARM_DIM] = arm_valid

    if not np.all(np.isfinite(state)):
        raise RuntimeError("state sanitization produced NaN or Inf")
    action = np.concatenate((state[1:], state[-1:]), axis=0)
    action_valid = np.zeros_like(state_valid)
    action_valid[:-1] = state_valid[1:]
    return state, state_valid, action, action_valid


def _append_episode_videos(
    writers: Dict[str, FfmpegVideoWriter],
    bag_path: Path,
    specs: list[umi.CameraSpec],
    plan: umi.EpisodePlan,
    *,
    image_size: Optional[int],
    source_shapes: Dict[str, tuple[int, int]],
) -> None:
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_message

    topics = [spec.image_topic for spec in specs]
    reader, topic_types = umi._open_reader(bag_path, topics)
    message_classes = {topic: get_message(topic_types[topic]) for topic in topics}
    spec_by_topic = {spec.image_topic: spec for spec in specs}
    wanted: Dict[str, Dict[int, list[int]]] = {}
    for spec in specs:
        by_source: Dict[int, list[int]] = {}
        for target, source in enumerate(plan.image_indices[spec.name]):
            by_source.setdefault(int(source), []).append(target)
        wanted[spec.name] = by_source
    source_index = {spec.name: 0 for spec in specs}
    written = {spec.name: 0 for spec in specs}
    while reader.has_next():
        topic, raw, _record_stamp_ns = reader.read_next()
        spec = spec_by_topic[topic]
        current_index = source_index[spec.name]
        target_indices = wanted[spec.name].get(current_index)
        source_index[spec.name] += 1
        if target_indices is None:
            continue
        message = deserialize_message(raw, message_classes[topic])
        image = decode_image(message, topic_types[topic])
        if image is None:
            raise umi.BagQualityError(f"failed to decode {topic} frame {current_index}")
        actual_shape = (int(image.shape[0]), int(image.shape[1]))
        if image_size is None and actual_shape != source_shapes[spec.name]:
            raise umi.BagQualityError(
                f"{spec.name} image resolution changed during the recording"
            )
        frame = umi._prepare_rgb(image, image_size)
        for _target_index in target_indices:
            writers[_video_key(spec, specs)].write(frame)
            written[spec.name] += 1
    missing = {
        name: len(plan.timestamps_ns) - count
        for name, count in written.items()
        if count != len(plan.timestamps_ns)
    }
    if missing:
        raise umi.BagQualityError(f"failed to write selected image frames: {missing}")


def _write_data_table(
    path: Path,
    states: np.ndarray,
    state_valid: np.ndarray,
    actions: np.ndarray,
    action_valid: np.ndarray,
    state_timestamps_ns: np.ndarray,
    video_timestamps_ns: Dict[str, np.ndarray],
    video_valid: Dict[str, np.ndarray],
    episode_lengths: list[int],
    fps: float,
) -> None:
    frame_indices = np.concatenate(
        [np.arange(length, dtype=np.int64) for length in episode_lengths]
    )
    episode_indices = np.concatenate(
        [np.full(length, index, dtype=np.int64) for index, length in enumerate(episode_lengths)]
    )
    timestamps = (frame_indices.astype(np.float32) / np.float32(fps)).astype(np.float32)
    total_frames = len(states)
    arrays = [
        _fixed_list(states, pa.float32()),
        _fixed_list(state_valid, pa.bool_()),
        _fixed_list(actions, pa.float32()),
        _fixed_list(action_valid, pa.bool_()),
        pa.array(timestamps, type=pa.float32()),
        pa.array(state_timestamps_ns, type=pa.int64()),
        pa.array(frame_indices, type=pa.int64()),
        pa.array(episode_indices, type=pa.int64()),
        pa.array(np.arange(total_frames), type=pa.int64()),
        pa.array(np.zeros(total_frames, dtype=np.int64), type=pa.int64()),
        pa.array(np.ones(total_frames, dtype=bool), type=pa.bool_()),
    ]
    names = [
        "observation.state",
        "observation.state_valid",
        "action",
        "action_valid",
        "timestamp",
        "observation.timestamp_ns",
        "frame_index",
        "episode_index",
        "index",
        "task_index",
        "valid.frame",
    ]
    for video_key in video_timestamps_ns:
        arrays.extend(
            (
                pa.array(video_timestamps_ns[video_key], type=pa.int64()),
                pa.array(video_valid[video_key], type=pa.bool_()),
            )
        )
        names.extend(
            (_video_timestamp_key(video_key), _video_valid_key(video_key))
        )
    table = pa.Table.from_arrays(arrays, names=names)
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = pq.ParquetWriter(path, table.schema, compression="zstd")
    try:
        start = 0
        for length in episode_lengths:
            writer.write_table(table.slice(start, length))
            start += length
    finally:
        writer.close()


def _write_tasks(path: Path, task: str) -> None:
    """Write the task string as the named pandas index expected by LeRobot."""

    table = pa.table(
        {
            "task_index": pa.array([0], type=pa.int64()),
            "task": pa.array([task], type=pa.string()),
        }
    )
    pandas_metadata = {
        "index_columns": ["task"],
        "column_indexes": [
            {
                "name": None,
                "field_name": None,
                "pandas_type": "unicode",
                "numpy_type": "object",
                "metadata": None,
            }
        ],
        "columns": [
            {
                "name": "task_index",
                "field_name": "task_index",
                "pandas_type": "int64",
                "numpy_type": "int64",
                "metadata": None,
            },
            {
                "name": "task",
                "field_name": "task",
                "pandas_type": "unicode",
                "numpy_type": "object",
                "metadata": None,
            },
        ],
        "creator": {"library": "pyarrow", "version": pa.__version__},
        "pandas_version": "2.0.0",
    }
    metadata = dict(table.schema.metadata or {})
    metadata[b"pandas"] = json.dumps(pandas_metadata).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table.replace_schema_metadata(metadata), path, compression="zstd")


def _video_feature(shape: tuple[int, int], fps: float) -> dict[str, object]:
    height, width = shape
    return {
        "dtype": "video",
        "shape": [height, width, 3],
        "names": ["height", "width", "channels"],
        "info": {
            "video.fps": float(fps),
            "video.height": height,
            "video.width": width,
            "video.channels": 3,
            "video.codec": "h264",
            "video.pix_fmt": "yuv420p",
            "video.is_depth_map": False,
            "has_audio": False,
        },
    }


def _modality(specs: list[umi.CameraSpec]) -> dict[str, object]:
    state = {}
    action = {}
    hands = _hand_specs(specs)
    for spec in hands:
        role = spec.role
        offset = 0 if len(hands) == 1 else DUAL_ARM_OFFSET[role]
        side = HAND_LABEL[role]
        prefix = "end_effector" if len(hands) == 1 else f"{side}_end_effector"
        blocks = {
            f"{prefix}_position": (offset, offset + 3, "m"),
            f"{prefix}_rotation_6d": (offset + 3, offset + 9, "rotation_6d"),
            ("gripper_width" if len(hands) == 1 else f"{side}_gripper_width"): (
                offset + 9,
                offset + 10,
                "m",
            ),
        }
        for name, (start, end, unit) in blocks.items():
            value = {"original_key": "observation.state", "indices": list(range(start, end)), "unit": unit}
            state[name] = value
            action[name] = {**value, "original_key": "action"}
    return {
        "state": state,
        "action": action,
        "video": {
            _video_key(spec, specs).removeprefix("observation.images."): {
                "original_key": _video_key(spec, specs),
                "source_camera": spec.name,
                "capture_role": spec.role,
                "timestamp_key": _video_timestamp_key(_video_key(spec, specs)),
                "validity_key": _video_valid_key(_video_key(spec, specs)),
            }
            for spec in specs
        },
        "annotation": {
            "task": {"original_key": "task_index", "metadata": "meta/tasks.parquet"}
        },
    }


def _directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def export_lerobot_dataset(
    bag_paths: list[Path],
    output_path: Path,
    *,
    camera_config: Path,
    calibration_path: Path,
    task: str,
    dataset_id: str,
    fps: float = 20.0,
    image_size: Optional[int] = 224,
    max_image_skew_ms: float = 40.0,
    minimum_frames: int = 24,
    episode_mode: str = "bag",
    camera_names: Optional[list[str]] = None,
) -> dict[str, object]:
    task = " ".join(task.split())
    if not task:
        raise ValueError("task text is required for a language-conditioned dataset")
    specs = umi.load_camera_specs(camera_config, camera_names)
    bag_paths = [path.resolve() for path in bag_paths]
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    planned: list[tuple[Path, umi.EpisodePlan]] = []
    source_shapes: Optional[Dict[str, tuple[int, int]]] = None
    for bag_index, bag_path in enumerate(bag_paths):
        print(
            f"DATASET_PROGRESS {bag_index + 1} {len(bag_paths)} scan {bag_path.name} 0",
            flush=True,
        )
        scan = umi.scan_bag(bag_path, specs, calibration_path)
        if source_shapes is None:
            source_shapes = dict(scan.image_shapes)
        elif scan.image_shapes != source_shapes:
            raise ValueError("camera resolutions differ between selected rosbags")
        plans = umi.build_episode_plans(
            bag_path.name,
            scan,
            specs,
            fps=fps,
            max_image_skew_ms=max_image_skew_ms,
            minimum_frames=minimum_frames,
            episode_mode=episode_mode,
            retain_invalid_images=True,
        )
        planned.extend((bag_path, plan) for plan in plans)
    if not planned or source_shapes is None:
        raise ValueError("no valid episodes were generated")

    state_dim, state_names = _state_schema(specs)
    output_shapes = {
        spec.name: (
            (image_size, image_size) if image_size is not None else source_shapes[spec.name]
        )
        for spec in specs
    }
    episode_lengths = [len(plan.timestamps_ns) for _, plan in planned]
    total_frames = sum(episode_lengths)
    state_parts = []
    state_valid_parts = []
    action_parts = []
    action_valid_parts = []
    state_timestamp_parts = []
    video_timestamp_parts: Dict[str, list[np.ndarray]] = {
        _video_key(spec, specs): [] for spec in specs
    }
    video_valid_parts: Dict[str, list[np.ndarray]] = {
        _video_key(spec, specs): [] for spec in specs
    }
    episode_rows = []
    frame_offset = 0
    video_offset = 0
    bag_indices = {path: index for index, path in enumerate(bag_paths)}

    with tempfile.TemporaryDirectory(prefix="lerobot_export_", dir=str(output_path.parent)) as temporary:
        root = Path(temporary) / "dataset"
        video_paths = {
            _video_key(spec, specs): root
            / "videos"
            / _video_key(spec, specs)
            / "chunk-000"
            / "file-000.mp4"
            for spec in specs
        }
        writers = {
            key: FfmpegVideoWriter(path, output_shapes[spec.name], fps)
            for spec in specs
            for key, path in [(_video_key(spec, specs), video_paths[_video_key(spec, specs)])]
        }
        try:
            for episode_index, (bag_path, plan) in enumerate(planned):
                bag_index = bag_indices[bag_path]
                print(
                    f"DATASET_PROGRESS {bag_index + 1} {len(bag_paths)} "
                    f"images {bag_path.name} {frame_offset}",
                    flush=True,
                )
                state, state_valid, action, action_valid = _episode_state(plan, specs)
                state_parts.append(state)
                state_valid_parts.append(state_valid)
                action_parts.append(action)
                action_valid_parts.append(action_valid)
                state_timestamp_parts.append(plan.timestamps_ns.astype(np.int64))
                for spec in specs:
                    video_key = _video_key(spec, specs)
                    video_timestamp_parts[video_key].append(
                        plan.image_timestamps_ns[spec.name].astype(np.int64)
                    )
                    video_valid_parts[video_key].append(
                        plan.image_valid[spec.name].astype(bool)
                    )
                state_stats = _feature_stats(state)
                action_stats = _feature_stats(action)
                _append_episode_videos(
                    writers,
                    bag_path,
                    specs,
                    plan,
                    image_size=image_size,
                    source_shapes=source_shapes,
                )
                length = len(state)
                row: dict[str, object] = {
                    "episode_index": episode_index,
                    "meta/episodes/chunk_index": 0,
                    "meta/episodes/file_index": 0,
                    "data/chunk_index": 0,
                    "data/file_index": 0,
                    "dataset_from_index": frame_offset,
                    "dataset_to_index": frame_offset + length,
                    "tasks": [task],
                    "length": length,
                    "source_bag": bag_path.name,
                    "source_segment_index": int(plan.segmentation["source_segment_index"]),
                    "stats/observation.state/min": state_stats["min"],
                    "stats/observation.state/max": state_stats["max"],
                    "stats/observation.state/mean": state_stats["mean"],
                    "stats/observation.state/std": state_stats["std"],
                    "stats/observation.state/count": [length],
                    "stats/action/min": action_stats["min"],
                    "stats/action/max": action_stats["max"],
                    "stats/action/mean": action_stats["mean"],
                    "stats/action/std": action_stats["std"],
                    "stats/action/count": [length],
                }
                for key in writers:
                    row[f"videos/{key}/chunk_index"] = 0
                    row[f"videos/{key}/file_index"] = 0
                    row[f"videos/{key}/from_timestamp"] = video_offset / fps
                    row[f"videos/{key}/to_timestamp"] = (video_offset + length) / fps
                episode_rows.append(row)
                frame_offset += length
                video_offset += length
            for writer in writers.values():
                writer.close()
        except Exception:
            for writer in writers.values():
                writer.abort()
            raise
        for key, writer in writers.items():
            if writer.frames != total_frames:
                raise RuntimeError(f"{key} wrote {writer.frames} frames, expected {total_frames}")

        states = np.concatenate(state_parts, axis=0)
        state_valid = np.concatenate(state_valid_parts, axis=0)
        actions = np.concatenate(action_parts, axis=0)
        action_valid = np.concatenate(action_valid_parts, axis=0)
        state_timestamps_ns = np.concatenate(state_timestamp_parts, axis=0)
        video_timestamps_ns = {
            key: np.concatenate(parts, axis=0)
            for key, parts in video_timestamp_parts.items()
        }
        video_valid = {
            key: np.concatenate(parts, axis=0)
            for key, parts in video_valid_parts.items()
        }
        print(
            f"DATASET_PROGRESS {len(bag_paths)} {len(bag_paths)} "
            f"package dataset {total_frames}",
            flush=True,
        )
        data_path = root / "data" / "chunk-000" / "file-000.parquet"
        _write_data_table(
            data_path,
            states,
            state_valid,
            actions,
            action_valid,
            state_timestamps_ns,
            video_timestamps_ns,
            video_valid,
            episode_lengths,
            fps,
        )
        tasks_path = root / "meta" / "tasks.parquet"
        _write_tasks(tasks_path, task)
        episode_columns = {name: [row[name] for row in episode_rows] for name in episode_rows[0]}
        episodes_path = root / "meta" / "episodes" / "chunk-000" / "file-000.parquet"
        episodes_path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table(episode_columns), episodes_path, compression="zstd")

        crop_boxes = (
            {spec.name: list(umi._fixed_square_roi(source_shapes[spec.name])) for spec in specs}
            if image_size is not None
            else {}
        )
        features: dict[str, object] = {
            "observation.state": {"dtype": "float32", "shape": [state_dim], "names": state_names},
            "observation.state_valid": {"dtype": "bool", "shape": [state_dim], "names": state_names},
            "action": {"dtype": "float32", "shape": [state_dim], "names": state_names},
            "action_valid": {"dtype": "bool", "shape": [state_dim], "names": state_names},
            "timestamp": {"dtype": "float32", "shape": [1], "names": None},
            "observation.timestamp_ns": {"dtype": "int64", "shape": [1], "names": None},
            "frame_index": {"dtype": "int64", "shape": [1], "names": None},
            "episode_index": {"dtype": "int64", "shape": [1], "names": None},
            "index": {"dtype": "int64", "shape": [1], "names": None},
            "task_index": {"dtype": "int64", "shape": [1], "names": None},
            "valid.frame": {"dtype": "bool", "shape": [1], "names": None},
        }
        for spec in specs:
            video_key = _video_key(spec, specs)
            features[video_key] = _video_feature(output_shapes[spec.name], fps)
            features[_video_timestamp_key(video_key)] = {
                "dtype": "int64",
                "shape": [1],
                "names": None,
            }
            features[_video_valid_key(video_key)] = {
                "dtype": "bool",
                "shape": [1],
                "names": None,
            }
        info = {
            "codebase_version": "v3.0",
            "schema_version": SCHEMA_VERSION,
            "robot_type": "insight_umi",
            "dataset_id": dataset_id,
            "total_episodes": len(planned),
            "total_frames": total_frames,
            "total_tasks": 1,
            "chunks_size": 1000,
            "data_files_size_in_mb": 100,
            "video_files_size_in_mb": 500,
            "fps": float(fps),
            "splits": {"train": f"0:{len(planned)}"},
            "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
            "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
            "features": features,
            "state_layout": (
                "single_arm_10d: xyz + rotation_6d(first two matrix rows) + gripper_width_m"
                if state_dim == ARM_DIM
                else "[left_10d, right_10d], each xyz + rotation_6d(first two matrix rows) + gripper_width_m"
            ),
            "action_layout": "absolute next-state target; final frame repeats final state with all-false action_valid",
            "video_processing": {
                "image_mode": "original" if image_size is None else "fixed_roi_square",
                "crop_policy": None if image_size is None else "bottom_center_max_square",
                "crop_boxes_xywh": crop_boxes,
                "resize": None if image_size is None else [image_size, image_size],
                "codec": "h264",
                "pixel_format": "yuv420p",
                "timestamps": {
                    "alignment_key": "observation.timestamp_ns",
                    "camera_timestamp_suffix": "_timestamp_ns",
                    "unit": "ns",
                    "source": "rosbag_record_timestamp",
                },
                "streams": {
                    _video_key(spec, specs): {
                        "source_camera": spec.name,
                        "crop_box_xywh": crop_boxes.get(spec.name),
                        "resize_hw": None
                        if image_size is None
                        else [image_size, image_size],
                        "source_channels": "rgb"
                        if spec.role == "head"
                        else "grayscale_replicated_to_rgb",
                    }
                    for spec in specs
                },
            },
            "invalid_policy": "retain aligned frames; use validity masks and valid.frame during training",
            "source": {
                "format_profile": "umi_absolute_ee_lerobot_v3",
                "bags": [path.name for path in bag_paths],
                "camera_mapping": {_video_key(spec, specs): spec.name for spec in specs},
                "pose_topics": {spec.name: spec.pose_topic for spec in specs if spec.pose_topic},
            },
        }
        _json_write(root / "meta" / "info.json", info)
        _json_write(root / "meta" / "modality.json", _modality(specs))
        _json_write(
            root / "meta" / "stats.json",
            {
                "observation.state": _feature_stats(states),
                "action": _feature_stats(actions),
                "timestamp": _feature_stats(
                    np.concatenate([np.arange(length) / fps for length in episode_lengths]).astype(np.float32)
                ),
            },
        )
        summary = {
            "format": "lerobot_v3_umi_absolute_ee",
            "dataset_id": dataset_id,
            "output_path": str(output_path),
            "episode_count": len(planned),
            "total_frames": total_frames,
            "duration_s": round(total_frames / fps, 3),
            "processing_seconds": round(time.perf_counter() - started, 3),
            "fps": float(fps),
            "task": task,
            "camera_order": [spec.name for spec in specs],
            "video_keys": list(writers),
            "state_dimension": state_dim,
            "action_semantics": "absolute_next_state",
            "missing_value_policy": "finite_placeholder_with_per_dimension_validity_mask",
            "final_action_policy": "repeat_final_state_with_all_false_action_valid",
            "gripper_semantics": "physical_jaw_width_m",
            "size_bytes": _directory_size(root),
        }
        _json_write(root / "meta" / "manifest.json", summary)
        if output_path.exists():
            shutil.rmtree(output_path)
        root.replace(output_path)

    print(f"DATASET_DONE {total_frames} {len(planned)} {output_path}", flush=True)
    return summary


def build_parser() -> argparse.ArgumentParser:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bags", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--task", required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--camera-config", type=Path, default=project_root / "config" / "cameras.json")
    parser.add_argument("--calibration", type=Path, default=project_root / "config" / "gripper_calibration.json")
    parser.add_argument("--fps", type=float, default=20.0)
    parser.add_argument("--image-size", default="224")
    parser.add_argument("--max-image-skew-ms", type=float, default=40.0)
    parser.add_argument("--minimum-frames", type=int, default=24)
    parser.add_argument("--episode-mode", choices=("bag", "auto_pause"), default="bag")
    parser.add_argument("--camera", action="append", dest="camera_names")
    return parser


def _image_size(value: str) -> Optional[int]:
    if value.strip().lower() == "original":
        return None
    size = int(value)
    if size <= 0:
        raise ValueError("image size must be positive")
    return size


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        export_lerobot_dataset(
            args.bags,
            args.output,
            camera_config=args.camera_config,
            calibration_path=args.calibration,
            task=args.task,
            dataset_id=args.dataset_id,
            fps=args.fps,
            image_size=_image_size(args.image_size),
            max_image_skew_ms=args.max_image_skew_ms,
            minimum_frames=args.minimum_frames,
            episode_mode=args.episode_mode,
            camera_names=args.camera_names,
        )
    except umi.BagQualityError as exc:
        print(f"DATASET_REJECTED_BAG {exc}", file=sys.stderr)
        return 2
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
