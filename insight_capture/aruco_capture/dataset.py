"""Uniform-grid QC and strict contiguous LeRobot v3 export."""

import json
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
from .capture import write_json


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
    video, ratios = {}, {}
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
            ratios["video." + name] = float(vf.mean()) if len(grid) else 0
    all_valid = valid.all(1)
    for _, mask in video.values():
        all_valid &= mask
    ratios["all_required"] = float(all_valid.mean()) if len(grid) else 0
    report = {
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
        previous = end


def export(path):
    meta, grid, state, video, good, report = align(path)
    segments = json.loads((path / "segments.json").read_text())
    validate_segments(segments, meta["duration_s"])
    fps = report["fps"]
    runs = []
    for segment in segments:
        selected = np.flatnonzero(
            good & (grid >= segment["start_s"]) & (grid < segment["end_s"])
        )
        for run in np.split(selected, np.flatnonzero(np.diff(selected) != 1) + 1):
            if len(run) >= 3:
                runs.append((run, segment["task"].strip()))
    if not runs:
        raise ValueError(
            "没有可导出片段：先标注任务，且每段至少连续 3 帧的视频、双臂位姿与开合度均有效"
        )
    destination = path / "lerobot"
    if destination.exists():
        raise ValueError("lerobot 已存在；请先移走旧导出目录后重试")
    tasks = list(dict.fromkeys(task for _, task in runs))
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
            for episode, (run, task) in enumerate(runs):
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
