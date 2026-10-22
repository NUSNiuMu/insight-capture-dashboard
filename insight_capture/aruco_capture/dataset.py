"""Uniform-grid QC and strict contiguous LeRobot v3 export."""

import fcntl
import json
import math
import shutil
import sqlite3
import tempfile
from pathlib import Path

import cv2
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from insight_capture.postprocess.datasets.lerobot import (
    ARM_STATE_NAMES,
    FfmpegVideoWriter,
    _directory_size,
    _feature_stats,
    _video_feature,
    _write_tasks,
)
from insight_capture.runtime.mapping.geometry import rotation_distance_deg
from .capture import write_json

# Ported from the auto_pause episode splitter in
# insight_capture/legacy/umi_zarr.py: a hand is "idle" once its linear,
# angular and gripper speed all sit below these thresholds.
DEFAULT_IDLE_DURATION_S = 0.8
DEFAULT_IDLE_LINEAR_SPEED_M_S = 0.02
DEFAULT_IDLE_ANGULAR_SPEED_DEG_S = 10.0
DEFAULT_IDLE_GRIPPER_SPEED_M_S = 0.005
DEFAULT_IDLE_GAP_TOLERANCE_S = 0.15
DEFAULT_MINIMUM_SEGMENT_DURATION_S = 1.0


def connect(path):
    return sqlite3.connect(f"file:{path / 'capture.sqlite3'}?mode=ro", uri=True)


def nearest(times, grid, tolerance, unique=False):
    if not len(times):
        return np.zeros(len(grid), dtype=int), np.zeros(len(grid), dtype=bool)
    right = np.clip(np.searchsorted(times, grid), 0, len(times) - 1)
    left = np.maximum(right - 1, 0)
    indices = np.where(abs(times[left] - grid) <= abs(times[right] - grid), left, right)
    valid = abs(times[indices] - grid) <= tolerance
    if unique:
        candidates = np.flatnonzero(valid)
        ordered = candidates[
            np.argsort(abs(times[indices[candidates]] - grid[candidates]))
        ]
        _, first = np.unique(indices[ordered], return_index=True)
        valid[:] = False
        valid[ordered[first]] = True
    return indices, valid


def _head_clock(rows):
    """Head frame times on the host timeline, plus the measured frame period.

    Insight9 header stamps are evenly spaced, while host receive times jitter
    by ~10 ms; shifting the stamps by the minimal observed receive latency
    keeps the stamps' spacing on the same timeline as the other host-stamped
    streams. Falls back to receive time if stamps are missing."""
    if not rows:
        return np.zeros(0), np.zeros(0, int), np.zeros(0, int), None
    rowid = np.array([r[0] for r in rows])
    receive = np.array([r[1] for r in rows], float)
    stamp = np.array([r[2] or 0 for r in rows], np.int64)
    if (stamp > 0).all():
        order = np.argsort(stamp, kind="stable")
        seconds = stamp[order] / 1e9
        times = seconds + np.percentile(receive[order] - seconds, 1)
    else:
        order = np.argsort(receive, kind="stable")
        times = receive[order]
    steps = np.diff(times)
    period = float(np.median(steps[steps > 0])) if (steps > 0).any() else None
    return times, rowid[order], stamp[order], period


def _offsets(times, indices, valid, reference):
    if not valid.any():
        return None
    gap = np.abs(times[indices[valid]] - reference[valid]) * 1000
    return {
        "p50": round(float(np.percentile(gap, 50)), 2),
        "p95": round(float(np.percentile(gap, 95)), 2),
        "max": round(float(gap.max()), 2),
    }


def align(path):
    meta = json.loads((path / "session.json").read_text())
    if meta["status"] != "stopped":
        raise ValueError("请先停止录制；中断录制保留原始 SQLite，暂不导出")
    config = meta["config"]
    fps = int(config.get("fps", 20))
    if fps <= 0 or fps > 120:
        raise ValueError("fps 必须在 1–120 范围内")
    grid = np.arange(int(np.ceil(meta["duration_s"] * fps))) / fps
    tolerance = config.get("sync_tolerance_ms", 40) / 1000
    state, valid = (
        np.zeros((len(grid), 20), np.float32),
        np.zeros((len(grid), 20), bool),
    )
    video, ratios, masks, offsets = {}, {}, {}, {}
    with connect(path) as db:
        # Insight9 is the master clock: each grid row is bound to the head
        # frame nearest to it (within half a frame period, so an fps that
        # equals or divides the camera rate never aliases), and every other
        # stream is then matched against that frame's own time — not the
        # nominal grid tick — so `sync_tolerance_ms` is the true
        # inter-device offset.
        head_times, head_rowids, head_stamps, period = _head_clock(
            db.execute(
                "SELECT rowid,t,stamp_ns FROM samples WHERE kind='image' AND name='head' ORDER BY t"
            ).fetchall()
        )
        head_window = (period or 1 / fps) / 2 + 0.002
        hi, hf = nearest(head_times, grid, head_window, unique=True)
        reference = np.where(hf, head_times[hi], grid) if len(head_times) else grid
        video["head"] = (
            head_rowids[hi] if len(head_times) else np.zeros(len(grid), int),
            hf,
        )
        masks["video.head"] = hf
        ratios["video.head"] = float(hf.mean()) if len(grid) else 0

        # Pose rows are written with the same source stamp as the head
        # image they were computed from, so they pair with it exactly.
        rows = db.execute(
            "SELECT t,stamp_ns,payload FROM samples WHERE kind='pose' ORDER BY t"
        ).fetchall()
        by_stamp = {r[1]: k for k, r in enumerate(rows) if r[1]}
        indices = np.zeros(len(grid), int)
        fresh = np.zeros(len(grid), bool)
        for i in np.flatnonzero(hf):
            k = by_stamp.get(int(head_stamps[hi[i]]))
            if k is not None:
                indices[i], fresh[i] = k, True
        if not by_stamp:
            indices, fresh = nearest(
                np.array([r[0] for r in rows]), reference, head_window, unique=True
            )
            fresh &= hf
        poses = [json.loads(r[2]) for r in rows]
        reasons = {"left": [None] * len(grid), "right": [None] * len(grid)}
        for side, offset in [("left", 0), ("right", 10)]:
            target = config["arms"][side]
            for i in np.flatnonzero(fresh):
                pose = poses[indices[i]].get(target, {})
                reasons[side][i] = pose.get("reason")
                if pose.get("valid"):
                    matrix = np.array(pose["matrix"])
                    if matrix.shape == (4, 4) and np.isfinite(matrix).all():
                        state[i, offset : offset + 9] = np.r_[
                            matrix[:3, 3], matrix[:2, :3].reshape(-1)
                        ]
                        valid[i, offset : offset + 9] = True
            rows_g = db.execute(
                "SELECT t,payload FROM samples WHERE kind='gripper' AND name=? ORDER BY t",
                (side,),
            ).fetchall()
            gripper_times = np.array([r[0] for r in rows_g])
            gi, gf = nearest(gripper_times, reference, tolerance)
            offsets["gripper." + side] = _offsets(gripper_times, gi, gf, reference)
            for i in np.flatnonzero(gf):
                value = json.loads(rows_g[gi[i]][1])
                if value.get("valid") and np.isfinite(value["width_m"]):
                    state[i, offset + 9], valid[i, offset + 9] = value["width_m"], True
            for key, mask in [
                ("position", valid[:, offset : offset + 3].all(1)),
                ("rotation", valid[:, offset + 3 : offset + 9].all(1)),
                ("gripper", valid[:, offset + 9]),
            ]:
                masks[side + "." + key] = mask
                ratios[side + "." + key] = float(mask.mean()) if len(grid) else 0
        for name in [x["name"] for x in config.get("rgb", [])]:
            rows_v = db.execute(
                "SELECT rowid,t FROM samples WHERE kind='image' AND name=? ORDER BY t",
                (name,),
            ).fetchall()
            image_times = np.array([r[1] for r in rows_v])
            vi, vf = nearest(image_times, reference, tolerance, unique=True)
            offsets["video." + name] = _offsets(image_times, vi, vf, reference)
            video[name] = (
                np.array([rows_v[i][0] for i in vi])
                if rows_v
                else np.zeros(len(grid), int),
                vf,
            )
            masks["video." + name] = vf
            ratios["video." + name] = float(vf.mean()) if len(grid) else 0
    all_valid = valid.all(1)
    for _, mask in video.values():
        all_valid &= mask
    ratios["all_required"] = float(all_valid.mean()) if len(grid) else 0
    masks["all_required"] = all_valid
    report = {
        "missing_intervals": {
            name: intervals(~mask, fps) for name, mask in masks.items()
        },
        "expected_frames": len(grid),
        "fps": fps,
        "duration_s": meta["duration_s"],
        "valid_ratios": ratios,
        "state_dimension_valid_ratios": dict(
            zip(
                [f"{s}.{n}" for s in ("left", "right") for n in ARM_STATE_NAMES],
                valid.mean(0).tolist() if len(grid) else [0] * 20,
            )
        ),
        "queue_drops": meta.get("queue_drops", {}),
        "error": meta.get("error"),
        "sync_policy": "rows anchored on Insight9 frames (source stamp); other streams nearest host receive time to that frame; head VIO interpolated at image source stamp",
        "sync_tolerance_ms": tolerance * 1000,
        "head_period_ms": round(period * 1000, 3) if period else None,
        "sync_offsets_ms": offsets,
    }
    return meta, grid, state, video, all_valid, report, reasons


def intervals(mask, fps):
    edges = np.diff(np.r_[False, mask, False].astype(int))
    return [
        [float(a / fps), float(b / fps)]
        for a, b in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1))
    ]


def export_runs(grid, good, segments):
    runs = []
    for segment in segments:
        selected = np.flatnonzero(
            good & (grid >= segment["start_s"]) & (grid < segment["end_s"])
        )
        for run in np.split(selected, np.flatnonzero(np.diff(selected) != 1) + 1):
            if len(run) >= 3:
                runs.append(
                    (run, segment["task"].strip(), segment.get("hands", "both"))
                )
    return runs


def validate_segments(segments, duration):
    if not isinstance(segments, list):
        raise ValueError("片段必须为列表")
    previous = 0.0
    for item in segments:
        start, end, task = float(item["start_s"]), float(item["end_s"]), item["task"]
        if not (
            np.isfinite([start, end]).all()
            and previous <= start < end <= duration + 1e-6
        ):
            raise ValueError("片段须按时间排序、不重叠，且位于录制范围内")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("任务文本不能为空")
        if item.get("hands", "both") not in ("left", "right", "both"):
            raise ValueError("参与手臂须为 left、right 或 both")
        previous = end


def _rotation_matrix_from_6d(row01):
    row0, row1 = row01[:3], row01[3:6]
    return np.array([row0, row1, np.cross(row0, row1)])


def suggest_pause_segments(
    grid,
    state,
    good,
    fps,
    *,
    idle_duration_s=DEFAULT_IDLE_DURATION_S,
    idle_linear_speed_m_s=DEFAULT_IDLE_LINEAR_SPEED_M_S,
    idle_angular_speed_deg_s=DEFAULT_IDLE_ANGULAR_SPEED_DEG_S,
    idle_gripper_speed_m_s=DEFAULT_IDLE_GRIPPER_SPEED_M_S,
    idle_gap_tolerance_s=DEFAULT_IDLE_GAP_TOLERANCE_S,
    minimum_duration_s=DEFAULT_MINIMUM_SEGMENT_DURATION_S,
    duration_s=None,
):
    """建议以双臂静止时刻为界的片段草稿，供人工标注前预填任务文本。

    静止检测移植自 main 分支 `insight_capture/legacy/umi_zarr.py` 的
    `auto_pause` episode 切分：双臂线速度、角速度、开合速度同时低于阈值
    并持续 `idle_duration_s` 以上时视为一次停顿，在停顿区间中点切一刀。
    只在 `good`（双臂位姿、开合度与全部视频同时有效）的相邻帧之间判断
    静止，缺失或跳变数据不会被误判为停顿。返回的片段 `task` 留空，
    必须由人工填写后才能通过 `validate_segments` 保存。
    """
    frame_count = len(grid)
    if frame_count < 2:
        return []
    stationary = np.ones(frame_count - 1, dtype=bool)
    stationary &= good[:-1] & good[1:]
    for offset in (0, 10):
        position = state[:, offset : offset + 3].astype(np.float64)
        rotation = np.stack(
            [_rotation_matrix_from_6d(row) for row in state[:, offset + 3 : offset + 9]]
        )
        opening = state[:, offset + 9].astype(np.float64)
        linear_speed = np.linalg.norm(np.diff(position, axis=0), axis=1) * fps
        angular_speed = (
            np.array(
                [
                    rotation_distance_deg(rotation[i], rotation[i + 1])
                    for i in range(frame_count - 1)
                ]
            )
            * fps
        )
        gripper_speed = np.abs(np.diff(opening)) * fps
        stationary &= linear_speed <= idle_linear_speed_m_s
        stationary &= angular_speed <= idle_angular_speed_deg_s
        stationary &= gripper_speed <= idle_gripper_speed_m_s

    maximum_gap_intervals = int(math.floor(idle_gap_tolerance_s * fps))
    moving_padded = np.concatenate(([False], ~stationary, [False])).astype(np.int8)
    moving_changes = np.flatnonzero(np.diff(moving_padded)).reshape(-1, 2)
    for start, end in moving_changes:
        if end - start <= maximum_gap_intervals and start > 0 and end < len(stationary):
            stationary[start:end] = True

    minimum_idle_intervals = max(1, int(math.ceil(idle_duration_s * fps)))
    minimum_frames = max(1, int(math.ceil(minimum_duration_s * fps)))
    padded = np.concatenate(([False], stationary, [False])).astype(np.int8)
    changes = np.flatnonzero(np.diff(padded)).reshape(-1, 2)
    cuts, previous_cut = [], 0
    for start, end in changes:
        interval_count = int(end - start)
        if interval_count < minimum_idle_intervals or start == 0 or end == len(stationary):
            continue
        cut = int((start + end + 1) // 2)
        if cut - previous_cut < minimum_frames or frame_count - cut < minimum_frames:
            continue
        cuts.append(cut)
        previous_cut = cut

    boundaries = [0, *cuts, frame_count]
    limit = float(duration_s) if duration_s is not None else float(grid[-1]) + 1.0 / fps
    segments = []
    for start_index, end_index in zip(boundaries[:-1], boundaries[1:]):
        # Trim each draft to the frames that actually have video and both
        # arms, so it never starts before the first usable frame or runs
        # past the last one (or past the end of the recording).
        usable = np.flatnonzero(good[start_index:end_index])
        if not len(usable):
            continue
        first, last = start_index + usable[0], start_index + usable[-1]
        segments.append(
            {
                "start_s": round(float(grid[first]), 3),
                # Floor, not round: rounding up could land a fraction of a
                # millisecond past the recording and fail validate_segments.
                "end_s": math.floor(min(float(grid[last]) + 1.0 / fps, limit) * 1000)
                / 1000,
                "task": "",
                "hands": "both",
            }
        )
    return segments


def _capture_role(name):
    if name == "head":
        return "head"
    if name.startswith("rgb_left"):
        return "left_hand"
    if name.startswith("rgb_right"):
        return "right_hand"
    return name


def _modality(video_names):
    """meta/modality.json for a fixed left+right session (offsets 0/10 into
    the 20-dim state, matching ARM_STATE_NAMES). Adapted from the merged
    UMI pipeline's ``_modality()`` in postprocess/datasets/lerobot.py, which
    targets a dynamic 1-or-2-hand ``CameraSpec`` model that doesn't apply
    here — this build only claims keys that exist in this module's own
    parquet output (e.g. ``action``, not ``actions``; no per-video
    timestamp/validity columns, since export() doesn't write any)."""
    state, action = {}, {}
    for side, offset in (("left", 0), ("right", 10)):
        blocks = {
            f"{side}_end_effector_position": (offset, offset + 3, "m"),
            f"{side}_end_effector_rotation_6d": (offset + 3, offset + 9, "rotation_6d"),
            f"{side}_gripper_width": (offset + 9, offset + 10, "m"),
        }
        for name, (start, end, unit) in blocks.items():
            state[name] = {
                "original_key": "observation.state",
                "indices": list(range(start, end)),
                "unit": unit,
            }
            action[name] = {**state[name], "original_key": "action"}
    return {
        "state": state,
        "action": action,
        "video": {
            name: {
                "original_key": "observation.images." + name,
                "source_camera": name,
                "capture_role": _capture_role(name),
            }
            for name in video_names
        },
        "annotation": {
            "task": {"original_key": "task_index", "metadata": "meta/tasks.parquet"}
        },
    }


def export(path):
    meta, grid, state, video, good, report, _reasons = align(path)
    segments = json.loads((path / "segments.json").read_text())
    validate_segments(segments, meta["duration_s"])
    fps = report["fps"]
    runs = export_runs(grid, good, segments)
    if not runs:
        raise ValueError(
            "没有可导出片段：先标注任务，且每段至少连续 3 帧的视频、双臂位姿与开合度均有效"
        )
    destination = path / "lerobot"
    if destination.exists():
        raise ValueError("lerobot 已存在；请先移走旧导出目录后重试")
    tasks = list(dict.fromkeys(task for _, task, _ in runs))
    features = {
        key: {
            "dtype": "float32",
            "shape": [20],
            "names": [f"{s}.{n}" for s in ("left", "right") for n in ARM_STATE_NAMES],
        }
        for key in ("observation.state", "action")
    }
    for key in ("timestamp", "source_time_s"):
        features[key] = {"dtype": "float32", "shape": [1], "names": None}
    for key in ("frame_index", "episode_index", "index", "task_index"):
        features[key] = {"dtype": "int64", "shape": [1], "names": None}
    features["valid.frame"] = {"dtype": "bool", "shape": [1], "names": None}
    with (
        tempfile.TemporaryDirectory(prefix=".export-", dir=path) as temporary,
        connect(path) as db,
    ):
        root = Path(temporary) / "lerobot"
        writers, columns, episodes, offset = {}, {key: [] for key in features}, [], 0
        try:
            for episode, (run, task, hands) in enumerate(runs):
                indices, following = run[:-1], run[1:]
                length = len(indices)
                values = {
                    "observation.state": state[indices],
                    "action": state[following],
                    "timestamp": np.arange(length, dtype=np.float32) / fps,
                    "source_time_s": grid[indices].astype(np.float32),
                    "frame_index": np.arange(length),
                    "episode_index": np.full(length, episode),
                    "index": np.arange(offset, offset + length),
                    "task_index": np.full(length, tasks.index(task)),
                    "valid.frame": np.ones(length, dtype=bool),
                }
                for key, value in values.items():
                    columns[key].extend(value.tolist())
                row = {
                    "episode_index": episode,
                    "length": length,
                    "tasks": [task],
                    "participating_hands": hands,
                    "dataset_from_index": offset,
                    "dataset_to_index": offset + length,
                    "data/chunk_index": 0,
                    "data/file_index": 0,
                    "meta/episodes/chunk_index": 0,
                    "meta/episodes/file_index": 0,
                    "source_start_s": float(grid[run[0]]),
                    "source_end_s": float(grid[run[-1]]),
                }
                for key in ("observation.state", "action"):
                    for stat, value in _feature_stats(values[key]).items():
                        row["stats/" + key + "/" + stat] = value
                for name, (rowids, _) in video.items():
                    key = "observation.images." + name
                    for i in indices:
                        payload = db.execute(
                            "SELECT payload FROM samples WHERE rowid=?",
                            (int(rowids[i]),),
                        ).fetchone()[0]
                        bgr = cv2.imdecode(
                            np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR
                        )
                        if bgr is None:
                            raise ValueError("无法解码已录制图像: " + name)
                        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                        if name not in writers:
                            writers[name] = FfmpegVideoWriter(
                                root / "videos" / key / "chunk-000/file-000.mp4",
                                rgb.shape[:2],
                                fps,
                            )
                            features[key] = _video_feature(rgb.shape[:2], fps)
                        writers[name].write(rgb)
                    row.update(
                        {
                            f"videos/{key}/chunk_index": 0,
                            f"videos/{key}/file_index": 0,
                            f"videos/{key}/from_timestamp": offset / fps,
                            f"videos/{key}/to_timestamp": (offset + length) / fps,
                        }
                    )
                episodes.append(row)
                offset += length
            for writer in writers.values():
                writer.close()
        except BaseException:
            for writer in writers.values():
                writer.abort()
            raise
        data = root / "data/chunk-000/file-000.parquet"
        data.parent.mkdir(parents=True)
        arrays = {
            key: pa.array(
                values,
                type=pa.list_(pa.float32(), 20)
                if key in ("observation.state", "action")
                else pa.float32()
                if key in ("timestamp", "source_time_s")
                else pa.bool_()
                if key == "valid.frame"
                else pa.int64(),
            )
            for key, values in columns.items()
        }
        pq.write_table(pa.table(arrays), data)
        episode_path = root / "meta/episodes/chunk-000/file-000.parquet"
        episode_path.parent.mkdir(parents=True)
        pq.write_table(pa.Table.from_pylist(episodes), episode_path)
        _write_tasks(root / "meta/tasks.parquet", tasks)
        write_json(
            root / "meta/info.json",
            {
                "codebase_version": "v3.0",
                "robot_type": "insight_aruco",
                "fps": fps,
                "total_episodes": len(episodes),
                "total_frames": offset,
                "total_tasks": len(tasks),
                "chunks_size": 1000,
                "data_files_size_in_mb": 100,
                "video_files_size_in_mb": 500,
                "splits": {"train": f"0:{len(episodes)}"},
                "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
                "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
                "features": features,
                "action_semantics": "next absolute TCP state; final observation omitted; no action crosses a gap or annotation boundary",
                "coordinate_frame": "recording first synchronized head IMU pose",
                "rotation_6d": "first two rotation matrix rows",
            },
        )
        write_json(
            root / "meta/stats.json",
            {
                key: _feature_stats(np.array(columns[key]))
                for key in ("observation.state", "action", "timestamp")
            },
        )
        write_json(
            root / "meta/source.json",
            {
                "session": path.name,
                "session_metadata": meta,
                "segments": segments,
                "qc": report,
            },
        )
        write_json(root / "meta/modality.json", _modality(video))
        marker_config = meta["config"]["cube_marker_relative_localization"]
        write_json(
            root / "meta/manifest.json",
            {
                "format": "lerobot_v3_aruco_absolute_ee",
                "dataset_id": f"aruco/{path.name}",
                "output_path": str(destination),
                "episode_count": len(episodes),
                "total_frames": offset,
                "duration_s": round(offset / fps, 3),
                "fps": float(fps),
                "camera_order": list(video),
                "video_keys": [f"observation.images.{name}" for name in video],
                "state_dimension": 20,
                "action_semantics": "absolute_next_state",
                "openpi_action_key": "action",
                "lerobot_action_compatibility_key": "action",
                "missing_value_policy": "strict_contiguous_valid_only",
                "final_action_policy": "final_frame_of_each_run_is_action_target_only_not_emitted_as_observation",
                "gripper_semantics": "physical_jaw_width_m",
                "pose_quality_gate": {
                    "min_inlier_ratio": marker_config["min_inlier_ratio"],
                    "max_reprojection_error_px": marker_config["max_reprojection_error_px"],
                    "min_depth_m": marker_config["min_depth_m"],
                    "max_depth_m": marker_config["max_depth_m"],
                    "sync_tolerance_ms": report["sync_tolerance_ms"],
                },
                "source": {
                    "format_profile": "x86_aruco_single_session",
                    "session": path.name,
                    "arm_bindings": meta["config"]["arms"],
                },
                "size_bytes": _directory_size(root),
            },
        )
        root.rename(destination)
    return {"path": str(destination), "episodes": len(episodes), "frames": offset}


def _dataset_lock(output_root):
    """Serialize concurrent export_to_dataset() calls targeting the same
    accumulating dataset folder. Caller must close() the returned handle
    (releases the flock) once done."""
    output_root.mkdir(parents=True, exist_ok=True)
    handle = open(output_root / ".export.lock", "w")
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def _chunk_dir_size_mb(directory):
    if not directory.exists():
        return 0.0
    return sum(f.stat().st_size for f in directory.iterdir() if f.is_file()) / (
        1024 * 1024
    )


def _place_built_file(base_dir, extension, size_cap_mb, built_path):
    """`built_path` already holds a fully-written file for this export
    call. Find the next chunk-XXX/file-YYY.<extension> slot under
    base_dir, rolling to a new chunk directory once the current one would
    exceed size_cap_mb, then move the file into place. Returns
    (chunk_index, file_index)."""
    new_size_mb = built_path.stat().st_size / (1024 * 1024)
    chunk_dirs = (
        sorted(
            (d for d in base_dir.glob("chunk-*") if d.is_dir()),
            key=lambda d: int(d.name.split("-")[1]),
        )
        if base_dir.exists()
        else []
    )
    chunk_index = int(chunk_dirs[-1].name.split("-")[1]) if chunk_dirs else 0
    if chunk_dirs and _chunk_dir_size_mb(chunk_dirs[-1]) + new_size_mb > size_cap_mb:
        chunk_index += 1
    chunk_dir = base_dir / f"chunk-{chunk_index:03d}"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(chunk_dir.glob(f"file-*.{extension}"))
    file_index = int(existing[-1].stem.split("-")[1]) + 1 if existing else 0
    shutil.move(str(built_path), str(chunk_dir / f"file-{file_index:03d}.{extension}"))
    return chunk_index, file_index


def _merge_stats(base, addition):
    """Combine two _feature_stats()-shaped dicts (parallel Welford merge:
    exact, doesn't need the original samples)."""
    if base is None:
        return addition
    merged = {}
    for key, add_value in addition.items():
        if key not in base:
            merged[key] = add_value
            continue
        old_value = base[key]
        n_a, n_b = np.array(old_value["count"], np.float64), np.array(
            add_value["count"], np.float64
        )
        n = n_a + n_b
        mean_a, mean_b = np.array(old_value["mean"]), np.array(add_value["mean"])
        delta = mean_b - mean_a
        mean = mean_a + delta * n_b / n
        m2 = (
            np.array(old_value["std"]) ** 2 * n_a
            + np.array(add_value["std"]) ** 2 * n_b
            + delta**2 * n_a * n_b / n
        )
        merged[key] = {
            "min": np.minimum(old_value["min"], add_value["min"]).tolist(),
            "max": np.maximum(old_value["max"], add_value["max"]).tolist(),
            "mean": mean.tolist(),
            "std": np.sqrt(m2 / n).tolist(),
            "count": n.astype(int).tolist(),
        }
    return merged


def list_datasets(root):
    """List accumulating LeRobot v3 dataset folders under ``root`` (each one
    a directory that export_to_dataset() has written a meta/manifest.json
    into)."""
    root = Path(root)
    if not root.is_dir():
        return []
    result = []
    for folder in sorted(root.iterdir()):
        manifest_path = folder / "meta/manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text())
        result.append(
            {
                "name": folder.name,
                "episode_count": manifest["episode_count"],
                "total_frames": manifest["total_frames"],
                "duration_s": manifest["duration_s"],
                "session_count": len(manifest["source_datasets"]),
                "size_bytes": manifest.get("size_bytes", 0),
            }
        )
    return result


def dataset_export_index(root):
    """Map every session name that has been merged into any dataset folder
    under ``root`` to the list of folder names it was merged into, in one
    pass (used to annotate the recordings list without re-scanning every
    dataset folder per recording)."""
    root = Path(root)
    index = {}
    if not root.is_dir():
        return index
    for folder in sorted(root.iterdir()):
        manifest_path = folder / "meta/manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text())
        for entry in manifest["source_datasets"]:
            index.setdefault(entry["source_session"], []).append(folder.name)
    return index


def dataset_detail(output_root):
    output_root = Path(output_root)
    manifest_path = output_root / "meta/manifest.json"
    if not manifest_path.is_file():
        raise ValueError("数据集文件夹不存在")
    manifest = json.loads(manifest_path.read_text())
    return {
        "name": output_root.name,
        "episode_count": manifest["episode_count"],
        "total_frames": manifest["total_frames"],
        "duration_s": manifest["duration_s"],
        "size_bytes": manifest.get("size_bytes", 0),
        "sessions": manifest["source_datasets"],
    }


def delete_dataset(output_root):
    output_root = Path(output_root)
    if not (output_root / "meta/manifest.json").is_file():
        raise ValueError("数据集文件夹不存在")
    lock = _dataset_lock(output_root)
    try:
        shutil.rmtree(output_root)
    finally:
        try:
            lock.close()
        except OSError:
            pass


def _episode_files(output_root):
    return sorted(
        (output_root / "meta/episodes").glob("chunk-*/file-*.parquet"),
        key=lambda p: (int(p.parent.name.split("-")[1]), int(p.stem.split("-")[1])),
    )


def delete_session_from_dataset(output_root, session_name):
    """Remove one previously-merged session's episodes, frames and private
    data/video files from an accumulating dataset folder, and renumber every
    later session's episode_index/frame index down so the dataset stays
    contiguous (required for LeRobot v3 readers that assume no gaps).

    Each export_to_dataset() call writes its own private data.parquet and
    per-camera mp4 files (never shared with another session), so those are
    simply deleted outright. Only meta/episodes (a table shared across
    sessions) and each later session's own data.parquet need their
    episode_index/index columns shifted down by this session's counts."""
    output_root = Path(output_root)
    lock = _dataset_lock(output_root)
    try:
        manifest_path = output_root / "meta/manifest.json"
        if not manifest_path.is_file():
            raise ValueError("数据集文件夹不存在")
        manifest = json.loads(manifest_path.read_text())
        source_datasets = manifest["source_datasets"]
        index = next(
            (i for i, e in enumerate(source_datasets) if e["source_session"] == session_name),
            None,
        )
        if index is None:
            raise ValueError(f"{session_name} 不在这个数据集文件夹里")
        entry = source_datasets[index]
        frame_offset = sum(e["frames"] for e in source_datasets[:index])
        deleted_frames, deleted_episodes = entry["frames"], entry["episodes"]
        boundary = frame_offset + deleted_frames

        info_path = output_root / "meta/info.json"
        info = json.loads(info_path.read_text())
        fps = info["fps"]

        own_data_files, own_video_files, shifted_data_files = set(), set(), set()
        for path in _episode_files(output_root):
            table = pq.read_table(path)
            rows = table.to_pylist()
            kept = []
            changed = False
            for row in rows:
                if row["source_session"] == session_name:
                    own_data_files.add((row["data/chunk_index"], row["data/file_index"]))
                    for field in row:
                        if field.startswith("videos/") and field.endswith("/chunk_index"):
                            key = field.split("/")[1]
                            own_video_files.add(
                                (key, row[field], row[f"videos/{key}/file_index"])
                            )
                    changed = True
                    continue
                if row["dataset_from_index"] >= boundary:
                    row["episode_index"] -= deleted_episodes
                    row["dataset_from_index"] -= deleted_frames
                    row["dataset_to_index"] -= deleted_frames
                    for field in row:
                        if field.startswith("videos/") and (
                            field.endswith("/from_timestamp")
                            or field.endswith("/to_timestamp")
                        ):
                            row[field] -= deleted_frames / fps
                    shifted_data_files.add(
                        (row["data/chunk_index"], row["data/file_index"])
                    )
                    changed = True
                kept.append(row)
            if not changed:
                continue
            if kept:
                pq.write_table(pa.Table.from_pylist(kept), path)
            else:
                path.unlink()

        for chunk, file in shifted_data_files:
            data_path = (
                output_root / "data" / f"chunk-{chunk:03d}" / f"file-{file:03d}.parquet"
            )
            table = pq.read_table(data_path)
            table = table.set_column(
                table.schema.get_field_index("episode_index"),
                "episode_index",
                pc.subtract(table.column("episode_index"), deleted_episodes),
            )
            table = table.set_column(
                table.schema.get_field_index("index"),
                "index",
                pc.subtract(table.column("index"), deleted_frames),
            )
            pq.write_table(table, data_path)

        for chunk, file in own_data_files:
            _remove_placed_file(
                output_root / "data" / f"chunk-{chunk:03d}" / f"file-{file:03d}.parquet"
            )
        for key, chunk, file in own_video_files:
            _remove_placed_file(
                output_root
                / "videos"
                / key
                / f"chunk-{chunk:03d}"
                / f"file-{file:03d}.mp4"
            )

        info["total_episodes"] -= deleted_episodes
        info["total_frames"] -= deleted_frames
        info["splits"] = {"train": f"0:{info['total_episodes']}"}
        write_json(info_path, info)

        remaining = sorted((output_root / "data").glob("chunk-*/file-*.parquet"))
        if remaining:
            combined = pa.concat_tables(
                [
                    pq.read_table(
                        p, columns=["observation.state", "action", "timestamp"]
                    )
                    for p in remaining
                ]
            )
            write_json(
                output_root / "meta/stats.json",
                {
                    key: _feature_stats(np.array(combined.column(key).to_pylist()))
                    for key in ("observation.state", "action", "timestamp")
                },
            )
        else:
            write_json(output_root / "meta/stats.json", {})

        del source_datasets[index]
        manifest["source_datasets"] = source_datasets
        manifest["episode_count"] = info["total_episodes"]
        manifest["total_frames"] = info["total_frames"]
        manifest["duration_s"] = round(info["total_frames"] / fps, 3)
        manifest["size_bytes"] = _directory_size(output_root)
        write_json(manifest_path, manifest)

        (output_root / f"meta/sessions/{session_name}.json").unlink(missing_ok=True)
    finally:
        lock.close()
    return {
        "deleted_session": session_name,
        "remaining_episodes": info["total_episodes"],
        "remaining_frames": info["total_frames"],
    }


def _remove_placed_file(path):
    path.unlink(missing_ok=True)
    if path.parent.exists() and not any(path.parent.iterdir()):
        path.parent.rmdir()


def export_to_dataset(path, output_root):
    """Append this session's episodes into a persistent, growing LeRobot v3
    dataset at ``output_root`` (created on first use). Unlike export(),
    which always makes a fresh throwaway ``<session>/lerobot/`` dir, this
    can be called for many different sessions against the same
    output_root over time — each call adds more data/video files and
    episode rows, rolling to a new chunk-XXX directory once the previous
    one exceeds the dataset's declared size caps. Raises if an existing
    dataset at output_root has a different fps or state layout (can't be
    accumulated together)."""
    meta, grid, state, video, good, report, _reasons = align(path)
    segments = json.loads((path / "segments.json").read_text())
    validate_segments(segments, meta["duration_s"])
    fps = report["fps"]
    runs = export_runs(grid, good, segments)
    if not runs:
        raise ValueError(
            "没有可导出片段：先标注任务，且每段至少连续 3 帧的视频、双臂位姿与开合度均有效"
        )
    output_root = Path(output_root)
    lock = _dataset_lock(output_root)
    try:
        (output_root / "meta").mkdir(parents=True, exist_ok=True)
        info_path = output_root / "meta/info.json"
        existing_info = (
            json.loads(info_path.read_text()) if info_path.is_file() else None
        )
        if existing_info is not None:
            state_dim = existing_info["features"]["observation.state"]["shape"][0]
            if state_dim != 20 or existing_info["fps"] != fps:
                raise ValueError(
                    f"目标数据集 fps/状态维度与本次不一致（已有 {existing_info['fps']}Hz/"
                    f"{state_dim} 维，本次 {fps}Hz/20 维），不能累加到同一目录"
                )
        chunks_size = existing_info["chunks_size"] if existing_info else 1000
        data_cap_mb = existing_info["data_files_size_in_mb"] if existing_info else 100
        video_cap_mb = (
            existing_info["video_files_size_in_mb"] if existing_info else 500
        )

        manifest_path = output_root / "meta/manifest.json"
        existing_manifest = (
            json.loads(manifest_path.read_text()) if manifest_path.is_file() else None
        )
        source_datasets = (
            list(existing_manifest["source_datasets"]) if existing_manifest else []
        )
        if any(entry["source_session"] == path.name for entry in source_datasets):
            raise ValueError(
                f"{path.name} 已经导出到这个数据集文件夹过一次，不能重复累加（会导致数据重复计数）"
            )

        tasks_path = output_root / "meta/tasks.parquet"
        existing_tasks = (
            pq.read_table(tasks_path).sort_by("task_index").column("task").to_pylist()
            if tasks_path.is_file()
            else []
        )
        session_tasks = list(dict.fromkeys(task for _, task, _ in runs))
        all_tasks = existing_tasks + [
            task for task in session_tasks if task not in existing_tasks
        ]
        task_index_of = {task: index for index, task in enumerate(all_tasks)}

        episode_offset = existing_info["total_episodes"] if existing_info else 0
        frame_offset = existing_info["total_frames"] if existing_info else 0

        columns = {
            key: []
            for key in (
                "observation.state",
                "action",
                "timestamp",
                "source_time_s",
                "frame_index",
                "episode_index",
                "index",
                "task_index",
                "valid.frame",
            )
        }
        episodes, writers, video_shapes, offset = [], {}, {}, 0
        with (
            tempfile.TemporaryDirectory(prefix=".export-", dir=output_root) as temporary,
            connect(path) as db,
        ):
            build_root = Path(temporary)
            try:
                for local_episode, (run, task, hands) in enumerate(runs):
                    indices, following = run[:-1], run[1:]
                    length = len(indices)
                    episode = episode_offset + local_episode
                    values = {
                        "observation.state": state[indices],
                        "action": state[following],
                        "timestamp": np.arange(length, dtype=np.float32) / fps,
                        "source_time_s": grid[indices].astype(np.float32),
                        "frame_index": np.arange(length),
                        "episode_index": np.full(length, episode),
                        "index": np.arange(
                            frame_offset + offset, frame_offset + offset + length
                        ),
                        "task_index": np.full(length, task_index_of[task]),
                        "valid.frame": np.ones(length, dtype=bool),
                    }
                    for key, value in values.items():
                        columns[key].extend(value.tolist())
                    row = {
                        "episode_index": episode,
                        "length": length,
                        "tasks": [task],
                        "participating_hands": hands,
                        "dataset_from_index": frame_offset + offset,
                        "dataset_to_index": frame_offset + offset + length,
                        "source_start_s": float(grid[run[0]]),
                        "source_end_s": float(grid[run[-1]]),
                        "source_session": path.name,
                    }
                    for key in ("observation.state", "action"):
                        for stat, value in _feature_stats(values[key]).items():
                            row["stats/" + key + "/" + stat] = value
                    for name, (rowids, _) in video.items():
                        key = "observation.images." + name
                        for i in indices:
                            payload = db.execute(
                                "SELECT payload FROM samples WHERE rowid=?",
                                (int(rowids[i]),),
                            ).fetchone()[0]
                            bgr = cv2.imdecode(
                                np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR
                            )
                            if bgr is None:
                                raise ValueError("无法解码已录制图像: " + name)
                            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                            if name not in writers:
                                writers[name] = FfmpegVideoWriter(
                                    build_root / f"{name}.mp4", rgb.shape[:2], fps
                                )
                                video_shapes[name] = rgb.shape[:2]
                            writers[name].write(rgb)
                        row[f"videos/{key}/from_timestamp"] = (
                            frame_offset + offset
                        ) / fps
                        row[f"videos/{key}/to_timestamp"] = (
                            frame_offset + offset + length
                        ) / fps
                    episodes.append(row)
                    offset += length
                for writer in writers.values():
                    writer.close()
            except BaseException:
                for writer in writers.values():
                    writer.abort()
                raise

            data_built = build_root / "data.parquet"
            arrays = {
                key: pa.array(
                    values,
                    type=pa.list_(pa.float32(), 20)
                    if key in ("observation.state", "action")
                    else pa.float32()
                    if key in ("timestamp", "source_time_s")
                    else pa.bool_()
                    if key == "valid.frame"
                    else pa.int64(),
                )
                for key, values in columns.items()
            }
            pq.write_table(pa.table(arrays), data_built)
            data_chunk, data_file = _place_built_file(
                output_root / "data", "parquet", data_cap_mb, data_built
            )
            for row in episodes:
                row["data/chunk_index"] = data_chunk
                row["data/file_index"] = data_file

            for name in video:
                built = build_root / f"{name}.mp4"
                if not built.is_file():
                    continue
                key = "observation.images." + name
                video_chunk, video_file = _place_built_file(
                    output_root / "videos" / key, "mp4", video_cap_mb, built
                )
                for row in episodes:
                    row[f"videos/{key}/chunk_index"] = video_chunk
                    row[f"videos/{key}/file_index"] = video_file

        # meta/episodes stays one growing table under chunk-000, splitting
        # into a new file only once row count would exceed chunks_size.
        episodes_dir = output_root / "meta/episodes/chunk-000"
        existing_files = (
            sorted(episodes_dir.glob("file-*.parquet")) if episodes_dir.exists() else []
        )
        previous_episodes, episodes_file = [], 0
        if existing_files:
            episodes_file = int(existing_files[-1].stem.split("-")[1])
            previous_table = pq.read_table(existing_files[-1])
            if previous_table.num_rows + len(episodes) > chunks_size:
                episodes_file += 1
            else:
                previous_episodes = previous_table.to_pylist()
        for row in episodes:
            row["meta/episodes/chunk_index"] = 0
            row["meta/episodes/file_index"] = episodes_file
        episodes_dir.mkdir(parents=True, exist_ok=True)
        pq.write_table(
            pa.Table.from_pylist(previous_episodes + episodes),
            episodes_dir / f"file-{episodes_file:03d}.parquet",
        )

        _write_tasks(tasks_path, all_tasks)

        total_episodes = episode_offset + len(episodes)
        total_frames = frame_offset + offset
        features = dict(existing_info["features"]) if existing_info else {}
        features.update(
            {
                key: {
                    "dtype": "float32",
                    "shape": [20],
                    "names": [
                        f"{s}.{n}" for s in ("left", "right") for n in ARM_STATE_NAMES
                    ],
                }
                for key in ("observation.state", "action")
            }
        )
        for key in ("timestamp", "source_time_s"):
            features[key] = {"dtype": "float32", "shape": [1], "names": None}
        for key in ("frame_index", "episode_index", "index", "task_index"):
            features[key] = {"dtype": "int64", "shape": [1], "names": None}
        features["valid.frame"] = {"dtype": "bool", "shape": [1], "names": None}
        for name, shape in video_shapes.items():
            features["observation.images." + name] = _video_feature(shape, fps)

        write_json(
            info_path,
            {
                "codebase_version": "v3.0",
                "robot_type": "insight_aruco",
                "fps": fps,
                "total_episodes": total_episodes,
                "total_frames": total_frames,
                "total_tasks": len(all_tasks),
                "chunks_size": chunks_size,
                "data_files_size_in_mb": data_cap_mb,
                "video_files_size_in_mb": video_cap_mb,
                "splits": {"train": f"0:{total_episodes}"},
                "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
                "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
                "features": features,
                "action_semantics": "next absolute TCP state; final observation omitted; no action crosses a gap or annotation boundary",
                "coordinate_frame": "each session's own first synchronized head IMU pose (not shared across sessions)",
                "rotation_6d": "first two rotation matrix rows",
            },
        )
        stats_path = output_root / "meta/stats.json"
        existing_stats = (
            json.loads(stats_path.read_text()) if stats_path.is_file() else None
        )
        write_json(
            stats_path,
            _merge_stats(
                existing_stats,
                {
                    key: _feature_stats(np.array(columns[key]))
                    for key in ("observation.state", "action", "timestamp")
                },
            ),
        )
        write_json(output_root / "meta/modality.json", _modality(list(video)))
        source_datasets.append(
            {
                "source_session": path.name,
                "episodes": len(episodes),
                "frames": offset,
                "tasks": session_tasks,
            }
        )
        marker_config = meta["config"]["cube_marker_relative_localization"]
        write_json(
            manifest_path,
            {
                "format": "lerobot_v3_aruco_absolute_ee_merged",
                "dataset_id": f"aruco/{output_root.name}",
                "output_path": str(output_root),
                "episode_count": total_episodes,
                "total_frames": total_frames,
                "duration_s": round(total_frames / fps, 3),
                "fps": float(fps),
                "camera_order": list(video),
                "video_keys": [f"observation.images.{name}" for name in video],
                "state_dimension": 20,
                "action_semantics": "absolute_next_state",
                "openpi_action_key": "action",
                "lerobot_action_compatibility_key": "action",
                "missing_value_policy": "strict_contiguous_valid_only",
                "final_action_policy": "final_frame_of_each_run_is_action_target_only_not_emitted_as_observation",
                "gripper_semantics": "physical_jaw_width_m",
                "pose_quality_gate": {
                    "min_inlier_ratio": marker_config["min_inlier_ratio"],
                    "max_reprojection_error_px": marker_config["max_reprojection_error_px"],
                    "min_depth_m": marker_config["min_depth_m"],
                    "max_depth_m": marker_config["max_depth_m"],
                    "sync_tolerance_ms": report["sync_tolerance_ms"],
                },
                "tasks": all_tasks,
                "source_datasets": source_datasets,
                "size_bytes": _directory_size(output_root),
            },
        )
        (output_root / "meta/sessions").mkdir(parents=True, exist_ok=True)
        write_json(
            output_root / f"meta/sessions/{path.name}.json",
            {"segments": segments, "qc": report},
        )
    finally:
        lock.close()
    return {
        "path": str(output_root),
        "episodes_added": len(episodes),
        "frames_added": offset,
        "total_episodes": episode_offset + len(episodes),
        "total_frames": frame_offset + offset,
    }
