"""Apply delivery task annotations without touching cached video or hand poses."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .spec import DeliverySpec, validate_segments


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def apply_annotations(dataset: Path, spec: DeliverySpec) -> None:
    """Replace complete-coverage task metadata on an existing base dataset."""

    data_path = dataset / "data/chunk-000/file-000.parquet"
    table = pq.read_table(data_path)
    validate_segments(spec.segments, table.num_rows)
    task_index = np.empty(table.num_rows, dtype=np.int64)
    segment_rows = []
    for segment in spec.segments:
        task_index[segment.start_frame : segment.end_frame + 1] = segment.segment_index
        segment_rows.append(
            {
                "episode_index": 0,
                "segment_index": segment.segment_index,
                "task_index": segment.segment_index,
                "subtask": segment.subtask,
                "atomic_action": segment.atomic_action,
                "task": segment.task,
                "start_frame": segment.start_frame,
                "end_frame": segment.end_frame,
                "start_time_s": segment.start_frame / spec.fps,
                "end_time_s_exclusive": (segment.end_frame + 1) / spec.fps,
                "frame_count": segment.frame_count,
            }
        )
    column = table.schema.get_field_index("task_index")
    if column < 0:
        table = table.append_column("task_index", pa.array(task_index))
    else:
        table = table.set_column(column, "task_index", pa.array(task_index))
    pq.write_table(table, data_path, compression="zstd")
    tasks = [segment.task for segment in spec.segments]
    pq.write_table(
        pa.table({"task_index": np.arange(len(tasks), dtype=np.int64), "task": tasks}),
        dataset / "meta/tasks.parquet",
        compression="zstd",
    )
    pq.write_table(
        pa.Table.from_pylist(segment_rows),
        dataset / "meta/segments.parquet",
        compression="zstd",
    )
    episode_path = dataset / "meta/episodes/chunk-000/file-000.parquet"
    episode = pq.read_table(episode_path)
    tasks_column = episode.schema.get_field_index("tasks")
    if tasks_column >= 0:
        episode = episode.set_column(
            tasks_column,
            "tasks",
            pa.array([tasks], type=episode.schema.field(tasks_column).type),
        )
        pq.write_table(episode, episode_path, compression="zstd")

    info_path = dataset / "meta/info.json"
    info = json.loads(info_path.read_text(encoding="utf-8"))
    info["dataset_id"] = spec.dataset_id
    info["total_tasks"] = len(tasks)
    _write_json(info_path, info)
    manifest_path = dataset / "meta/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update({"dataset_id": spec.dataset_id, "task": spec.task, "tasks": tasks})
    manifest["annotation"] = {
        "boundary_convention": "inclusive frame ranges; complete coverage with no gap or overlap",
        "levels": ["subtask", "atomic_action"],
        "segments_path": "meta/segments.parquet",
        "segments": segment_rows,
    }
    _write_json(manifest_path, manifest)
