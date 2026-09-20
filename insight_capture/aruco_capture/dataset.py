"""Uniform-grid QC and strict contiguous LeRobot v3 export."""

import json
import math
import sqlite3
import tempfile
from pathlib import Path

import cv2
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from insight_capture.postprocess.datasets.lerobot import (
    ARM_STATE_NAMES,
    FfmpegVideoWriter,
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
    video, ratios, masks = {}, {}, {}
    with connect(path) as db:
        rows = db.execute(
            "SELECT t,payload FROM samples WHERE kind='pose' ORDER BY t"
        ).fetchall()
        indices, fresh = nearest(
            np.array([r[0] for r in rows]), grid, tolerance, unique=True
        )
        poses = [json.loads(r[1]) for r in rows]
        for side, offset in [("left", 0), ("right", 10)]:
            target = config["arms"][side]
            for i in np.flatnonzero(fresh):
                pose = poses[indices[i]].get(target, {})
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
            gi, gf = nearest(np.array([r[0] for r in rows_g]), grid, tolerance)
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
        for name in ["head"] + [x["name"] for x in config.get("rgb", [])]:
            rows_v = db.execute(
                "SELECT rowid,t FROM samples WHERE kind='image' AND name=? ORDER BY t",
                (name,),
            ).fetchall()
            vi, vf = nearest(
                np.array([r[1] for r in rows_v]), grid, tolerance, unique=True
            )
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
        "sync_policy": "nearest host receive time; head VIO interpolated at image source stamp",
        "sync_tolerance_ms": tolerance * 1000,
    }
    return meta, grid, state, video, all_valid, report


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
    segments = []
    for start_index, end_index in zip(boundaries[:-1], boundaries[1:]):
        if end_index <= start_index:
            continue
        end_s = (
            float(grid[end_index])
            if end_index < frame_count
            else float(grid[-1]) + 1.0 / fps
        )
        segments.append(
            {
                "start_s": float(grid[start_index]),
                "end_s": end_s,
                "task": "",
                "hands": "both",
            }
        )
    return segments


def export(path):
    meta, grid, state, video, good, report = align(path)
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
        root.rename(destination)
    return {"path": str(destination), "episodes": len(episodes), "frames": offset}
