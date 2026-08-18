"""Fast, deterministic checks for cached and newly generated deliveries."""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pyarrow.parquet as pq

from .spec import DeliverySpec, validate_segments


VIDEO_KEYS = (
    "observation.images.head_rgb",
    "observation.images.hand_left",
    "observation.images.hand_right",
)


def _video_frames(path: Path) -> tuple[int, float]:
    capture = cv2.VideoCapture(str(path))
    frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = float(capture.get(cv2.CAP_PROP_FPS))
    capture.release()
    return frames, fps


def audit_dataset(path: Path, spec: DeliverySpec, *, decode_videos: bool = False) -> dict[str, object]:
    """Raise on a delivery mismatch and return a compact audit record."""

    for relative in ("meta/info.json", "meta/schema.json", "meta/quality.json", "meta/quality.md", "meta/keyframes.parquet", "meta/segments.parquet", "meta/tasks.parquet"):
        if not (path / relative).is_file():
            raise ValueError(f"delivery is missing {relative}")
    data_path = path / "data/chunk-000/file-000.parquet"
    table = pq.read_table(data_path)
    frames = table.num_rows
    validate_segments(spec.segments, frames)
    required = {
        "observation.state",
        "action",
        "observation.hand_keypoints_3d",
        "observation.hand_keypoints_2d",
        "observation.hand_valid",
        "task_index",
    }
    missing = sorted(required - set(table.column_names))
    if missing:
        raise ValueError(f"delivery parquet is missing columns: {', '.join(missing)}")
    task_index = np.asarray(table["task_index"].to_pylist(), dtype=np.int64)
    for segment in spec.segments:
        actual = task_index[segment.start_frame : segment.end_frame + 1]
        if not np.all(actual == segment.segment_index):
            raise ValueError(f"task_index mismatch in segment {segment.segment_index}")

    manifest = json.loads((path / "meta/manifest.json").read_text(encoding="utf-8"))
    if bool(manifest.get("wrist_view_hand_detections_delivered", True)):
        raise ValueError("wrist-view hand detections must not be delivered")
    camera = json.loads((path / "meta/camera_params.json").read_text(encoding="utf-8"))
    serialized = json.dumps(camera, ensure_ascii=False).lower()
    if "right_tcp" in serialized or "left_tcp" in serialized:
        raise ValueError("camera parameters contain forbidden TCP transforms")

    videos = {}
    for key in VIDEO_KEYS:
        video = path / f"videos/{key}/chunk-000/file-000.mp4"
        count, fps = _video_frames(video)
        if count != frames or abs(fps - spec.fps) > 0.05:
            raise ValueError(f"video mismatch for {key}: {count} frames at {fps:g} fps")
        if decode_videos:
            capture = cv2.VideoCapture(str(video))
            decoded = 0
            while capture.grab():
                decoded += 1
            capture.release()
            if decoded != frames:
                raise ValueError(f"video decode mismatch for {key}: {decoded}/{frames}")
        videos[key] = {"frames": count, "fps": fps}

    return {
        "status": "PASS",
        "frames": frames,
        "segments": len(spec.segments),
        "columns": len(table.column_names),
        "videos": videos,
    }
