"""Generate a review video from delivered head-view hand landmarks."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pyarrow.parquet as pq


EDGES = (
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (0, 9), (9, 10), (10, 11), (11, 12),
    (0, 13), (13, 14), (14, 15), (15, 16),
    (0, 17), (17, 18), (18, 19), (19, 20),
)


def write_overlay(dataset: Path, output: Path) -> None:
    table = pq.read_table(dataset / "data/chunk-000/file-000.parquet")
    points = np.asarray(table["observation.hand_keypoints_2d"].to_pylist(), np.float32).reshape(-1, 2, 21, 2)
    valid = np.asarray(table["observation.hand_keypoints_2d_valid"].to_pylist(), bool).reshape(-1, 2, 21)
    task = np.asarray(table["task_index"].to_pylist(), np.int64)
    source = dataset / "videos/observation.images.head_rgb/chunk-000/file-000.mp4"
    capture = cv2.VideoCapture(str(source))
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(capture.get(cv2.CAP_PROP_FPS))
    output.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(output), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    colors = ((64, 220, 64), (64, 128, 255))
    frame_index = 0
    while frame_index < len(points):
        ok, frame = capture.read()
        if not ok:
            break
        for hand in range(2):
            if not np.all(valid[frame_index, hand]):
                continue
            xy = np.rint(points[frame_index, hand]).astype(int)
            for start, end in EDGES:
                cv2.line(frame, tuple(xy[start]), tuple(xy[end]), colors[hand], 2, cv2.LINE_AA)
            for point in xy:
                cv2.circle(frame, tuple(point), 3, colors[hand], -1, cv2.LINE_AA)
            cv2.putText(frame, ("L", "R")[hand], tuple(xy[0]), cv2.FONT_HERSHEY_SIMPLEX, 0.7, colors[hand], 2)
        cv2.putText(frame, f"frame {frame_index}  segment {task[frame_index]}", (20, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
        writer.write(frame)
        frame_index += 1
    capture.release()
    writer.release()
    if frame_index != len(points):
        raise RuntimeError(f"overlay decoded {frame_index}/{len(points)} frames")
