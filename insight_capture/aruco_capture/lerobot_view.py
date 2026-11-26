"""Read a LeRobot dataset (v3.0, or v2.x with per-episode files) for playback.

Only reads: the dataset folder is never written. The page asks for the
summary (episodes, cameras, state names), then one episode at a time (its
state / action rows and, per camera, which mp4 holds it and from what time),
and streams the mp4 files themselves.
"""

import json
from pathlib import Path

import numpy as np
import pyarrow.compute as pc
import pyarrow.parquet as pq

# Per-frame columns sent to the page; anything else in data/*.parquet stays.
ROW_KEYS = ("observation.state", "action")


def resolve(root, value):
    """A dataset folder from a folder name under ``root`` (the export folder)
    or an absolute path on this machine."""
    value = str(value or "").strip()
    if not value:
        raise ValueError("请选择数据集文件夹或填写路径")
    path = Path(value).expanduser() if value.startswith(("/", "~")) else Path(root) / value
    path = path.resolve()
    if not (path / "meta/info.json").is_file():
        raise ValueError(f"{path} 不是 LeRobot 数据集（缺少 meta/info.json）")
    return path


def _info(path):
    return json.loads((path / "meta/info.json").read_text())


def _version(info):
    return str(info.get("codebase_version", "")).lstrip("v")


def _jsonl(file):
    return [json.loads(line) for line in file.read_text().splitlines() if line.strip()]


def _tasks(path, info):
    """task_index -> task text."""
    if (path / "meta/tasks.parquet").is_file():
        table = pq.read_table(path / "meta/tasks.parquet").to_pandas()
        if "task" not in table.columns:   # v3 keeps the text as the index
            table = table.reset_index().rename(columns={"index": "task"})
        return {int(r["task_index"]): str(r["task"]) for _, r in table.iterrows()}
    if (path / "meta/tasks.jsonl").is_file():
        return {int(r["task_index"]): str(r["task"]) for r in _jsonl(path / "meta/tasks.jsonl")}
    return {}


def _episodes(path, info):
    """Episode rows (dicts) in episode order."""
    if _version(info).startswith("3"):
        files = sorted(
            (path / "meta/episodes").glob("chunk-*/file-*.parquet"),
            key=lambda p: (int(p.parent.name.split("-")[1]), int(p.stem.split("-")[1])),
        )
        rows = [row for f in files for row in pq.read_table(f).to_pylist()]
    elif (path / "meta/episodes.jsonl").is_file():
        rows = _jsonl(path / "meta/episodes.jsonl")
    else:
        raise ValueError("找不到 episode 元数据（meta/episodes）")
    return sorted(rows, key=lambda r: int(r["episode_index"]))


def _video_keys(info):
    return [k for k, f in info.get("features", {}).items() if f.get("dtype") == "video"]


def _clean(value):
    """JSON-safe copy of a metadata value (numpy scalars and arrays included)."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_clean(v) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def summary(path):
    info = _info(path)
    if not _version(info).startswith(("2", "3")):
        raise ValueError(f"不支持的 LeRobot 版本 {info.get('codebase_version')}（支持 v2.x 与 v3.0）")
    tasks = _tasks(path, info)
    episodes = []
    for row in _episodes(path, info):
        names = row.get("tasks")
        if isinstance(names, str):
            names = [names]
        extra = {
            k: _clean(v) for k, v in row.items()
            if k in ("subtask", "subtask_id", "task_id", "participating_hands", "source_session",
                     "source_start_s", "source_end_s")
        }
        episodes.append({
            "index": int(row["episode_index"]),
            "length": int(row["length"]),
            "tasks": [str(t) for t in (names if names is not None else [])],
            **extra,
        })
    features = info.get("features", {})
    return {
        "path": str(path),
        "name": path.name,
        "version": info.get("codebase_version", ""),
        "robot_type": info.get("robot_type", ""),
        "fps": float(info["fps"]),
        "total_episodes": len(episodes),
        "total_frames": int(info.get("total_frames", sum(e["length"] for e in episodes))),
        "video_keys": _video_keys(info),
        "names": {k: _clean(features[k].get("names")) for k in ROW_KEYS if k in features},
        "tasks": {str(k): v for k, v in tasks.items()},
        "episodes": episodes,
    }


def _episode_row(path, info, index):
    for row in _episodes(path, info):
        if int(row["episode_index"]) == index:
            return row
    raise ValueError(f"数据集中没有 episode {index}")


def episode(path, index):
    info = _info(path)
    index = int(index)
    row = _episode_row(path, info, index)
    fps = float(info["fps"])
    v3 = _version(info).startswith("3")
    chunks = int(info.get("chunks_size", 1000))
    if v3:
        data = path / info["data_path"].format(
            chunk_index=int(row.get("data/chunk_index", 0)), file_index=int(row.get("data/file_index", 0))
        )
    else:
        data = path / info["data_path"].format(episode_chunk=index // chunks, episode_index=index)
    columns = set(pq.read_schema(data).names)
    wanted = [k for k in (*ROW_KEYS, "timestamp", "frame_index", "task_index", "valid.frame", "episode_index")
              if k in columns]
    table = pq.read_table(data, columns=wanted)
    if "episode_index" in columns:
        table = table.filter(pc.equal(table["episode_index"], index))
    if "frame_index" in columns:
        table = table.sort_by("frame_index")
    values = {k: table[k].to_pylist() for k in wanted if k != "episode_index"}
    count = table.num_rows
    timestamps = values.get("timestamp") or [i / fps for i in range(count)]
    start = timestamps[0] if timestamps else 0.0
    rows = []
    for i in range(count):
        item = {"t": round(float(timestamps[i]) - start, 6)}
        for key in ROW_KEYS:
            if key in values:
                item[key] = _clean(values[key][i])
        if "task_index" in values:
            item["task_index"] = int(values["task_index"][i])
        item["valid"] = bool(values["valid.frame"][i]) if "valid.frame" in values else True
        rows.append(item)
    videos = {}
    for key in _video_keys(info):
        if v3:
            chunk = int(row.get(f"videos/{key}/chunk_index", 0))
            file = int(row.get(f"videos/{key}/file_index", 0))
            relative = info["video_path"].format(video_key=key, chunk_index=chunk, file_index=file)
            begin = float(row.get(f"videos/{key}/from_timestamp", 0.0))
            end = float(row.get(f"videos/{key}/to_timestamp", begin + count / fps))
        else:
            relative = info["video_path"].format(
                episode_chunk=index // chunks, video_key=key, episode_index=index
            )
            begin, end = 0.0, count / fps
        if (path / relative).is_file():
            videos[key] = {"file": relative, "from": begin, "to": end}
    return {"index": index, "fps": fps, "length": count, "rows": rows, "videos": videos}


def video_file(path, relative):
    """An mp4 inside the dataset folder (never outside it)."""
    file = (path / str(relative)).resolve()
    if file.suffix != ".mp4" or path not in file.parents or not file.is_file():
        raise ValueError("视频文件不存在")
    return file
