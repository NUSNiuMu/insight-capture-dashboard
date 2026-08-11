#!/usr/bin/env python3

"""Build original-resolution, atomically labelled cup-grasp LeRobot datasets."""

from __future__ import annotations

import argparse
import copy
import json
import math
import sys
from dataclasses import replace
from pathlib import Path
from typing import Dict, Iterable, Optional

import cv2
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

import lerobot_dataset_export as lerobot
import umi_dataset_export as umi


ATOMIC_LABELS = (
    ("reach", "approach_cup", "Move both grippers toward the disposable cup."),
    ("grasp", "close_grippers", "Close both grippers around the disposable cup."),
    ("transport", "transport_cup_to_box", "Lift and carry the disposable cup to the box."),
    (
        "release",
        "open_grippers",
        "Open both grippers to release the disposable cup into the box.",
    ),
    ("retreat", "retreat_from_cup", "Move both grippers away from the released cup."),
)
DEFAULT_MINIMUM_GRIPPER_RANGE_M = 0.03
DEFAULT_PRE_ROLL_S = 2.0
DEFAULT_POST_ROLL_S = 2.5
DEFAULT_MAX_TRANSIENT_OPEN_S = 0.6
DEFAULT_MAX_CLOSE_ACTION_S = 0.6


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _smooth_width(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64).reshape(-1)
    if len(values) < 5:
        return values.copy()
    padded = np.pad(values, (2, 2), mode="edge")
    return np.asarray(
        [np.median(padded[index : index + 5]) for index in range(len(values))],
        dtype=np.float64,
    )


def _fill_short_false_gaps(mask: np.ndarray, maximum_frames: int) -> np.ndarray:
    result = np.asarray(mask, dtype=bool).copy()
    index = 0
    while index < len(result):
        if result[index]:
            index += 1
            continue
        end = index
        while end < len(result) and not result[end]:
            end += 1
        if index > 0 and end < len(result) and end - index <= maximum_frames:
            result[index:end] = True
        index = end
    return result


def infer_atomic_boundaries(
    width_m: np.ndarray,
    *,
    fps: float,
    minimum_range_m: float = DEFAULT_MINIMUM_GRIPPER_RANGE_M,
    maximum_transient_open_s: float = DEFAULT_MAX_TRANSIENT_OPEN_S,
    maximum_close_action_s: float = DEFAULT_MAX_CLOSE_ACTION_S,
) -> tuple[list[int], Dict[str, float]]:
    """Return five complete action ranges from one open-close-open width trace."""

    values = _smooth_width(width_m)
    if len(values) < 10:
        raise ValueError("episode is too short for atomic cup-action labels")
    opened = float(np.percentile(values, 90))
    closed = float(np.percentile(values, 5))
    width_range = opened - closed
    if not np.isfinite(width_range) or width_range < minimum_range_m:
        raise ValueError(
            f"gripper range {width_range:.4f} m is below {minimum_range_m:.4f} m"
        )
    open_gate = closed + 0.75 * width_range
    edge_frames = min(len(values), max(3, int(round(0.5 * fps))))
    if float(np.median(values[:edge_frames])) < open_gate:
        raise ValueError("episode does not start with an open gripper")
    if float(np.median(values[-edge_frames:])) < open_gate:
        raise ValueError("episode does not end with an open gripper")

    closed_mask = values < closed + 0.50 * width_range
    closed_mask = _fill_short_false_gaps(
        closed_mask,
        max(1, int(round(maximum_transient_open_s * fps))),
    )
    closed_indices = np.flatnonzero(closed_mask)
    if not len(closed_indices):
        raise ValueError("episode has no sustained closed-gripper interval")
    close_mid = int(closed_indices[0])
    release_mid = int(closed_indices[-1] + 1)
    if release_mid >= len(values):
        raise ValueError("episode has no release after closing")

    before = np.flatnonzero(values[: close_mid + 1] >= opened - 0.10 * width_range)
    close_start = int(before[-1] + 1) if len(before) else max(1, close_mid - 1)
    close_end_candidates = np.flatnonzero(
        values[close_start:release_mid] <= closed + 0.20 * width_range
    )
    close_end = (
        close_start + int(close_end_candidates[0])
        if len(close_end_candidates)
        else close_mid + 1
    )
    close_start = max(
        close_start,
        close_end - max(1, int(round(maximum_close_action_s * fps))),
    )

    low_before_release = np.flatnonzero(
        values[close_end:release_mid] <= closed + 0.20 * width_range
    )
    release_start = (
        close_end + int(low_before_release[-1])
        if len(low_before_release)
        else max(close_end + 1, release_mid - 1)
    )
    open_after = np.flatnonzero(
        values[release_mid:] >= opened - 0.10 * width_range
    )
    release_end = (
        release_mid + int(open_after[0])
        if len(open_after)
        else release_mid + max(1, int(round(0.5 * fps)))
    )
    release_end = max(
        release_end,
        release_start + max(1, int(round(0.5 * fps))),
    )

    boundaries = [0, close_start, close_end, release_start, release_end, len(values)]
    for index in range(1, len(boundaries)):
        boundaries[index] = max(boundaries[index], boundaries[index - 1] + 1)
    boundaries[-1] = len(values)
    if any(right <= left for left, right in zip(boundaries, boundaries[1:])):
        raise ValueError(f"atomic action boundaries are invalid: {boundaries}")
    return boundaries, {
        "opened_m": opened,
        "closed_m": closed,
        "range_m": width_range,
    }


def _width_candidates(plan: umi.EpisodePlan) -> Iterable[tuple[str, np.ndarray]]:
    for key, value in plan.lowdim.items():
        if key.endswith("_gripper_width"):
            yield key, np.asarray(value, dtype=np.float64).reshape(-1)


def _slice_plan(
    plan: umi.EpisodePlan,
    segment: slice,
    *,
    fps: float,
    primary_width_key: str,
) -> umi.EpisodePlan:
    original_start_s = float(plan.segmentation["source_start_s"])
    segmentation = copy.deepcopy(plan.segmentation)
    segmentation.update(
        {
            "mode": "cup_grasp",
            "source_start_s": round(original_start_s + segment.start / fps, 6),
            "source_end_s": round(original_start_s + segment.stop / fps, 6),
            "reviewed_from_auto_segment": int(
                plan.segmentation["source_segment_index"]
            ),
            "primary_gripper_width_key": primary_width_key,
            "selection_reason": "complete open-close-open cup grasp",
        }
    )
    sliced = replace(
        plan,
        timestamps_ns=plan.timestamps_ns[segment],
        image_indices={key: value[segment] for key, value in plan.image_indices.items()},
        image_timestamps_ns={
            key: value[segment] for key, value in plan.image_timestamps_ns.items()
        },
        image_valid={key: value[segment] for key, value in plan.image_valid.items()},
        lowdim={key: value[segment] for key, value in plan.lowdim.items()},
        segmentation=segmentation,
    )
    boundaries, metrics = infer_atomic_boundaries(
        sliced.lowdim[primary_width_key], fps=fps
    )
    sliced.segmentation["atomic_boundaries"] = boundaries
    sliced.segmentation["gripper_cycle"] = metrics
    return sliced


class CupEpisodeSelector:
    """Select and trim complete cup grasps from pause-split UMI plans."""

    def __init__(
        self,
        *,
        fps: float,
        minimum_frames: int,
        minimum_gripper_range_m: float = DEFAULT_MINIMUM_GRIPPER_RANGE_M,
        pre_roll_s: float = DEFAULT_PRE_ROLL_S,
        post_roll_s: float = DEFAULT_POST_ROLL_S,
    ) -> None:
        self.fps = fps
        self.minimum_frames = minimum_frames
        self.minimum_gripper_range_m = minimum_gripper_range_m
        self.pre_roll_s = pre_roll_s
        self.post_roll_s = post_roll_s
        self.selected: list[umi.EpisodePlan] = []
        self.sources: list[Dict[str, object]] = []
        self.rejected: list[Dict[str, object]] = []
        self.pose_rejected_segments: list[Dict[str, object]] = []

    def __call__(
        self, bag_path: Path, plans: list[umi.EpisodePlan]
    ) -> list[umi.EpisodePlan]:
        selected_for_bag: list[umi.EpisodePlan] = []
        if plans:
            self.pose_rejected_segments.extend(
                {
                    "source_bag": bag_path.name,
                    **item,
                }
                for item in plans[0].segmentation.get("rejected_segments", [])
            )
        for plan in plans:
            segment_index = int(plan.segmentation["source_segment_index"])
            candidates = list(_width_candidates(plan))
            if not candidates:
                self.rejected.append(
                    {
                        "source_bag": bag_path.name,
                        "source_segment_index": segment_index,
                        "reason": "no gripper width stream",
                    }
                )
                continue
            primary_key, primary_width = max(
                candidates,
                key=lambda item: float(
                    np.percentile(item[1], 90) - np.percentile(item[1], 5)
                ),
            )
            try:
                boundaries, metrics = infer_atomic_boundaries(
                    primary_width,
                    fps=self.fps,
                    minimum_range_m=self.minimum_gripper_range_m,
                )
            except ValueError as exc:
                self.rejected.append(
                    {
                        "source_bag": bag_path.name,
                        "source_segment_index": segment_index,
                        "reason": str(exc),
                    }
                )
                continue

            start = max(
                0,
                boundaries[1] - int(round(self.pre_roll_s * self.fps)),
            )
            stop = min(
                len(primary_width),
                boundaries[4] + int(round(self.post_roll_s * self.fps)),
            )
            if stop - start < self.minimum_frames:
                self.rejected.append(
                    {
                        "source_bag": bag_path.name,
                        "source_segment_index": segment_index,
                        "reason": "trimmed episode is shorter than minimum_frames",
                    }
                )
                continue
            try:
                selected = _slice_plan(
                    plan,
                    slice(start, stop),
                    fps=self.fps,
                    primary_width_key=primary_key,
                )
            except ValueError as exc:
                self.rejected.append(
                    {
                        "source_bag": bag_path.name,
                        "source_segment_index": segment_index,
                        "reason": f"trimmed episode: {exc}",
                    }
                )
                continue
            selected.segmentation["pre_roll_s"] = self.pre_roll_s
            selected.segmentation["post_roll_s"] = self.post_roll_s
            selected.segmentation["untrimmed_gripper_cycle"] = metrics
            selected_for_bag.append(selected)
            self.selected.append(selected)
            self.sources.append(
                {
                    "source_bag": bag_path.name,
                    "source_segment_index": segment_index,
                    "source_start_s": selected.segmentation["source_start_s"],
                    "source_end_s": selected.segmentation["source_end_s"],
                    "frames": len(selected.timestamps_ns),
                    "primary_gripper_width_key": primary_key,
                }
            )
        return selected_for_bag

    def report(self) -> Dict[str, object]:
        return {
            "mode": "cup_grasp",
            "status": "PASS_WITH_SOURCE_EXCLUSIONS",
            "accepted_episode_count": len(self.selected),
            "accepted_episodes": self.sources,
            "excluded_non_grasp_or_incomplete_segments": self.rejected,
            "rejected_pose_discontinuity_segments": self.pose_rejected_segments,
            "thresholds": {
                "minimum_gripper_range_m": self.minimum_gripper_range_m,
                "pre_roll_s": self.pre_roll_s,
                "post_roll_s": self.post_roll_s,
                "max_position_step_m": umi.DEFAULT_MAX_POSITION_STEP_M,
                "position_jump_policy": umi.POSITION_JUMP_POLICY,
                "max_orientation_step_deg": umi.DEFAULT_MAX_ORIENTATION_STEP_DEG,
                "max_pose_gap_ms": umi.DEFAULT_MAX_POSE_GAP_MS,
            },
        }


def apply_atomic_annotations(
    dataset: Path,
    plans: list[umi.EpisodePlan],
    *,
    full_task: str,
    fps: float,
    quality_report: Dict[str, object],
) -> list[Dict[str, object]]:
    """Write reusable semantic task IDs and complete per-episode segments."""

    data_path = dataset / "data/chunk-000/file-000.parquet"
    table = pq.read_table(data_path)
    episode_ids = np.asarray(table["episode_index"].to_pylist(), dtype=np.int64)
    if len(plans) != len(np.unique(episode_ids)):
        raise ValueError("selected plan count does not match exported episode count")
    task_index = np.empty(table.num_rows, dtype=np.int64)
    segment_rows: list[Dict[str, object]] = []
    global_segment_index = 0
    for episode_index, plan in enumerate(plans):
        selected = np.flatnonzero(episode_ids == episode_index)
        boundaries = [int(value) for value in plan.segmentation["atomic_boundaries"]]
        if boundaries[0] != 0 or boundaries[-1] != len(selected):
            raise ValueError(f"episode {episode_index} annotation length mismatch")
        source_start_s = float(plan.segmentation["source_start_s"])
        for label_index, ((subtask, atomic_action, task), start, stop) in enumerate(
            zip(ATOMIC_LABELS, boundaries[:-1], boundaries[1:])
        ):
            task_index[selected[start:stop]] = label_index
            segment_rows.append(
                {
                    "episode_index": episode_index,
                    "segment_index": global_segment_index,
                    "task_index": label_index,
                    "subtask": subtask,
                    "atomic_action": atomic_action,
                    "task": task,
                    "start_frame": start,
                    "end_frame": stop - 1,
                    "start_time_s": start / fps,
                    "end_time_s_exclusive": stop / fps,
                    "source_start_s": source_start_s + start / fps,
                    "source_end_s_exclusive": source_start_s + stop / fps,
                    "frame_count": stop - start,
                    "boundary_method": "pause_split_plus_gripper_width_transition",
                }
            )
            global_segment_index += 1

    column = table.schema.get_field_index("task_index")
    table = table.set_column(
        column,
        "task_index",
        pa.array(task_index, type=pa.int64()),
    )
    temporary_data = data_path.with_suffix(".annotating.parquet")
    pq.write_table(table, temporary_data, compression="zstd")
    temporary_data.replace(data_path)

    tasks = [item[2] for item in ATOMIC_LABELS]
    pq.write_table(
        pa.table(
            {
                "task_index": np.arange(len(tasks), dtype=np.int64),
                "task": tasks,
            }
        ),
        dataset / "meta/tasks.parquet",
        compression="zstd",
    )
    pq.write_table(
        pa.Table.from_pylist(segment_rows),
        dataset / "meta/segments.parquet",
        compression="zstd",
    )

    episodes_path = dataset / "meta/episodes/chunk-000/file-000.parquet"
    episodes = pq.read_table(episodes_path)
    tasks_column = episodes.schema.get_field_index("tasks")
    episodes = episodes.set_column(
        tasks_column,
        "tasks",
        pa.array(
            [tasks for _ in range(episodes.num_rows)],
            type=episodes.schema.field(tasks_column).type,
        ),
    )
    pq.write_table(episodes, episodes_path, compression="zstd")

    info_path = dataset / "meta/info.json"
    info = json.loads(info_path.read_text(encoding="utf-8"))
    info["total_tasks"] = len(tasks)
    info["annotation_processing"] = {
        "levels": ["subtask", "atomic_action"],
        "segments_path": "meta/segments.parquet",
        "boundary_convention": (
            "episode-local inclusive frame ranges; complete coverage without gaps or overlaps"
        ),
    }
    _write_json(info_path, info)

    modality_path = dataset / "meta/modality.json"
    modality = json.loads(modality_path.read_text(encoding="utf-8"))
    modality.setdefault("annotation", {})["segments"] = {
        "metadata": "meta/segments.parquet",
        "levels": ["subtask", "atomic_action"],
    }
    _write_json(modality_path, modality)

    _write_json(dataset / "meta/quality_report.json", quality_report)
    manifest_path = dataset / "meta/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["full_task"] = full_task
    manifest.pop("task", None)
    manifest["tasks"] = tasks
    manifest["annotation"] = {
        "boundary_convention": (
            "episode-local inclusive frame ranges; complete coverage without gaps or overlaps"
        ),
        "levels": ["subtask", "atomic_action"],
        "segments_path": "meta/segments.parquet",
        "segments": segment_rows,
    }
    manifest["quality_filtering"] = {
        **quality_report,
        "path": "meta/quality_report.json",
    }
    _write_json(manifest_path, manifest)
    return segment_rows


def write_review_contact_sheet(
    dataset: Path,
    segments: list[Dict[str, object]],
    *,
    maximum_episodes: int = 12,
) -> Optional[Path]:
    """Write compact representative frames without duplicating full videos."""

    info = json.loads((dataset / "meta/info.json").read_text(encoding="utf-8"))
    video_keys = [
        key
        for key, value in info["features"].items()
        if isinstance(value, dict) and value.get("dtype") == "video"
    ]
    preferred = "observation.images.base_0_rgb"
    video_key = preferred if preferred in video_keys else video_keys[0]
    video_path = dataset / f"videos/{video_key}/chunk-000/file-000.mp4"
    episodes = pq.read_table(
        dataset / "meta/episodes/chunk-000/file-000.parquet",
        columns=["episode_index", "dataset_from_index"],
    ).to_pylist()
    offsets = {
        int(row["episode_index"]): int(row["dataset_from_index"])
        for row in episodes
    }
    selected_segments = [
        segment
        for segment in segments
        if int(segment["episode_index"]) < maximum_episodes
    ]
    capture = cv2.VideoCapture(str(video_path))
    tiles = []
    tile_width, tile_height = 320, 272
    for segment in selected_segments:
        midpoint = (int(segment["start_frame"]) + int(segment["end_frame"])) // 2
        frame_index = offsets[int(segment["episode_index"])] + midpoint
        capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ok, frame = capture.read()
        if not ok:
            capture.release()
            raise RuntimeError(f"cannot decode review frame {frame_index}")
        frame = cv2.resize(frame, (tile_width, tile_height), interpolation=cv2.INTER_AREA)
        overlay = frame.copy()
        cv2.rectangle(overlay, (0, 0), (tile_width, 50), (0, 0, 0), -1)
        cv2.addWeighted(overlay, 0.72, frame, 0.28, 0.0, frame)
        cv2.putText(
            frame,
            f"Episode {segment['episode_index']}  t={segment['start_time_s']:.2f}s",
            (8, 19),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.46,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
        cv2.putText(
            frame,
            str(segment["atomic_action"]),
            (8, 41),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.44,
            (0, 255, 255),
            1,
            cv2.LINE_AA,
        )
        tiles.append(frame)
    capture.release()
    if not tiles:
        return None
    columns = len(ATOMIC_LABELS)
    rows = math.ceil(len(tiles) / columns)
    canvas = np.zeros((rows * tile_height, columns * tile_width, 3), dtype=np.uint8)
    for index, tile in enumerate(tiles):
        row, column = divmod(index, columns)
        canvas[
            row * tile_height : (row + 1) * tile_height,
            column * tile_width : (column + 1) * tile_width,
        ] = tile
    output = dataset / "review/atomic_action_contact_sheet.jpg"
    output.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output), canvas):
        raise RuntimeError(f"cannot write review contact sheet: {output}")
    return output


def verify_dataset(dataset: Path, *, decode_videos: bool = True) -> Dict[str, object]:
    """Validate episode, annotation, numeric, timestamp, and video integrity."""

    info = json.loads((dataset / "meta/info.json").read_text(encoding="utf-8"))
    data = pq.read_table(dataset / "data/chunk-000/file-000.parquet")
    episodes = pq.read_table(
        dataset / "meta/episodes/chunk-000/file-000.parquet"
    ).to_pylist()
    segments = pq.read_table(dataset / "meta/segments.parquet").to_pylist()
    tasks = pq.read_table(dataset / "meta/tasks.parquet")
    if data.num_rows != int(info["total_frames"]):
        raise ValueError("data row count does not match info total_frames")
    if len(episodes) != int(info["total_episodes"]):
        raise ValueError("episode row count does not match info total_episodes")
    if tasks.num_rows != int(info["total_tasks"]):
        raise ValueError("task row count does not match info total_tasks")
    states = np.asarray(data["observation.state"].to_pylist(), dtype=np.float32)
    actions = np.asarray(data["action"].to_pylist(), dtype=np.float32)
    if not np.all(np.isfinite(states)) or not np.all(np.isfinite(actions)):
        raise ValueError("state or action contains NaN/Inf")
    episode_ids = np.asarray(data["episode_index"].to_pylist(), dtype=np.int64)
    frame_ids = np.asarray(data["frame_index"].to_pylist(), dtype=np.int64)
    task_ids = np.asarray(data["task_index"].to_pylist(), dtype=np.int64)
    timestamps = np.asarray(
        data["observation.timestamp_ns"].to_pylist(), dtype=np.int64
    )
    maximum_pose_step_mm: Dict[str, Dict[str, float]] = {}
    for episode in episodes:
        episode_index = int(episode["episode_index"])
        length = int(episode["length"])
        selected = np.flatnonzero(episode_ids == episode_index)
        if len(selected) != length or not np.array_equal(
            frame_ids[selected], np.arange(length)
        ):
            raise ValueError(f"episode {episode_index} frame index mismatch")
        if not np.all(np.diff(timestamps[selected]) > 0):
            raise ValueError(f"episode {episode_index} timestamps are not increasing")
        episode_segments = [
            row for row in segments if int(row["episode_index"]) == episode_index
        ]
        expected_start = 0
        for segment in episode_segments:
            start = int(segment["start_frame"])
            end = int(segment["end_frame"])
            if start != expected_start or end < start:
                raise ValueError(
                    f"episode {episode_index} segment coverage breaks at {expected_start}"
                )
            actual = task_ids[selected[start : end + 1]]
            if not np.all(actual == int(segment["task_index"])):
                raise ValueError(f"episode {episode_index} task_index mismatch")
            expected_start = end + 1
        if expected_start != length:
            raise ValueError(
                f"episode {episode_index} segments cover {expected_start}/{length}"
            )
        maximum_pose_step_mm[str(episode_index)] = {
            "left": float(
                np.linalg.norm(np.diff(states[selected, 0:3], axis=0), axis=1).max()
                * 1000
            ),
            "right": float(
                np.linalg.norm(
                    np.diff(states[selected, 10:13], axis=0), axis=1
                ).max()
                * 1000
            ),
        }

    invalid_video_frames = {
        name: int(np.count_nonzero(~np.asarray(data[name].to_pylist(), dtype=bool)))
        for name in data.column_names
        if name.endswith("_rgb_valid")
    }
    videos: Dict[str, Dict[str, object]] = {}
    for key, feature in info["features"].items():
        if not isinstance(feature, dict) or feature.get("dtype") != "video":
            continue
        path = dataset / f"videos/{key}/chunk-000/file-000.mp4"
        capture = cv2.VideoCapture(str(path))
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        if decode_videos:
            frames = 0
            while capture.grab():
                frames += 1
        else:
            frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        capture.release()
        if frames != data.num_rows or abs(fps - float(info["fps"])) > 0.05:
            raise ValueError(f"video mismatch for {key}: {frames} frames at {fps:g} Hz")
        videos[key] = {
            "frames": frames,
            "fps": fps,
            "bytes": path.stat().st_size,
        }
    return {
        "status": "PASS",
        "episodes": len(episodes),
        "frames": data.num_rows,
        "tasks": tasks.num_rows,
        "segments": len(segments),
        "maximum_pose_step_mm": maximum_pose_step_mm,
        "invalid_video_frames": invalid_video_frames,
        "videos": videos,
    }


def update_cup_catalog(output_root: Path) -> Path:
    """Rebuild the cumulative catalog used to track progress toward 500 episodes."""

    datasets = []
    for manifest_path in sorted(output_root.glob("*/meta/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        quality = manifest.get("quality_filtering", {})
        if quality.get("mode") != "cup_grasp":
            continue
        dataset = manifest_path.parents[1]
        datasets.append(
            {
                "dataset_id": manifest.get("dataset_id"),
                "path": str(dataset),
                "source_bag": quality.get("source_bag"),
                "episodes": int(manifest.get("episode_count", 0)),
                "frames": int(manifest.get("total_frames", 0)),
                "duration_s": float(manifest.get("duration_s", 0.0)),
                "size_bytes": int(manifest.get("size_bytes", 0)),
                "verification": manifest.get("verification", {}).get("status"),
            }
        )
    catalog = {
        "schema_version": 1,
        "target_episode_count": 500,
        "dataset_count": len(datasets),
        "episode_count": sum(item["episodes"] for item in datasets),
        "remaining_episode_count": max(
            0, 500 - sum(item["episodes"] for item in datasets)
        ),
        "total_frames": sum(item["frames"] for item in datasets),
        "duration_s": round(sum(item["duration_s"] for item in datasets), 3),
        "size_bytes": sum(item["size_bytes"] for item in datasets),
        "datasets": datasets,
    }
    path = output_root / "cup_catalog.json"
    temporary = path.with_suffix(".writing.json")
    _write_json(temporary, catalog)
    temporary.replace(path)
    return path


def export_cup_dataset(
    bag_path: Path,
    output_path: Path,
    *,
    camera_config: Path,
    calibration_path: Path,
    task: str,
    dataset_id: str,
    fps: float = 20.0,
    minimum_frames: int = 24,
    camera_names: Optional[list[str]] = None,
    minimum_gripper_range_m: float = DEFAULT_MINIMUM_GRIPPER_RANGE_M,
    pre_roll_s: float = DEFAULT_PRE_ROLL_S,
    post_roll_s: float = DEFAULT_POST_ROLL_S,
    write_review: bool = True,
) -> Dict[str, object]:
    selector = CupEpisodeSelector(
        fps=fps,
        minimum_frames=minimum_frames,
        minimum_gripper_range_m=minimum_gripper_range_m,
        pre_roll_s=pre_roll_s,
        post_roll_s=post_roll_s,
    )
    summary = lerobot.export_lerobot_dataset(
        [bag_path],
        output_path,
        camera_config=camera_config,
        calibration_path=calibration_path,
        task=task,
        dataset_id=dataset_id,
        fps=fps,
        image_size=None,
        max_image_skew_ms=40.0,
        minimum_frames=minimum_frames,
        episode_mode="auto_pause",
        camera_names=camera_names,
        plan_transform=selector,
    )
    quality = selector.report()
    quality["source_bag"] = bag_path.name
    quality["image_processing"] = {
        "mode": "original",
        "crop": None,
        "resize": None,
    }
    for name in ("image_header_audit.json", "recording_network_audit.json"):
        path = bag_path / name
        if path.is_file():
            quality[name.removesuffix(".json")] = json.loads(
                path.read_text(encoding="utf-8")
            )
    segments = apply_atomic_annotations(
        output_path,
        selector.selected,
        full_task=task,
        fps=fps,
        quality_report=quality,
    )
    review_path = (
        write_review_contact_sheet(output_path, segments) if write_review else None
    )
    verification = verify_dataset(output_path, decode_videos=True)
    _write_json(output_path / "meta/verification.json", verification)
    manifest_path = output_path / "meta/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["verification"] = {
        **verification,
        "path": "meta/verification.json",
    }
    if review_path is not None:
        manifest["review_contact_sheet"] = str(
            review_path.relative_to(output_path)
        )
    manifest["size_bytes"] = sum(
        path.stat().st_size for path in output_path.rglob("*") if path.is_file()
    )
    manifest["catalog_path"] = "../cup_catalog.json"
    _write_json(manifest_path, manifest)
    update_cup_catalog(output_path.parent)
    summary = manifest
    print(
        f"CUP_PIPELINE_DONE {summary['total_frames']} "
        f"{summary['episode_count']} {output_path}",
        flush=True,
    )
    return summary


def build_parser() -> argparse.ArgumentParser:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bag", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--task", required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument(
        "--camera-config",
        type=Path,
        default=project_root / "config/cameras.json",
    )
    parser.add_argument(
        "--calibration",
        type=Path,
        default=project_root / "config/gripper_calibration.json",
    )
    parser.add_argument("--fps", type=float, default=20.0)
    parser.add_argument("--minimum-frames", type=int, default=24)
    parser.add_argument("--minimum-gripper-range-m", type=float, default=0.03)
    parser.add_argument("--pre-roll-s", type=float, default=2.0)
    parser.add_argument("--post-roll-s", type=float, default=2.5)
    parser.add_argument("--camera", action="append", dest="camera_names")
    parser.add_argument(
        "--image-size",
        default="original",
        help="compatibility option; cup production export only accepts original",
    )
    parser.add_argument(
        "--episode-mode",
        default="cup_grasp",
        help="compatibility option; cup production export only accepts cup_grasp",
    )
    parser.add_argument("--no-review", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if str(args.image_size).strip().lower() != "original":
        print("ERROR: cup production export requires original image resolution", file=sys.stderr)
        return 1
    if str(args.episode_mode).strip().lower() != "cup_grasp":
        print("ERROR: cup pipeline episode mode must be cup_grasp", file=sys.stderr)
        return 1
    if (
        args.fps <= 0
        or args.minimum_frames < 2
        or args.minimum_gripper_range_m <= 0
        or args.pre_roll_s < 0
        or args.post_roll_s < 0
    ):
        print("ERROR: invalid positive pipeline threshold", file=sys.stderr)
        return 1
    try:
        export_cup_dataset(
            args.bag.resolve(),
            args.output.resolve(),
            camera_config=args.camera_config.resolve(),
            calibration_path=args.calibration.resolve(),
            task=args.task,
            dataset_id=args.dataset_id,
            fps=args.fps,
            minimum_frames=args.minimum_frames,
            camera_names=args.camera_names,
            minimum_gripper_range_m=args.minimum_gripper_range_m,
            pre_roll_s=args.pre_roll_s,
            post_roll_s=args.post_roll_s,
            write_review=not args.no_review,
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
