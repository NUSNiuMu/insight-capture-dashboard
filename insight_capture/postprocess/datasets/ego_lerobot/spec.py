"""Input specification and action-segment validation for Ego delivery."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Segment:
    segment_index: int
    subtask: str
    atomic_action: str
    task: str
    start_frame: int
    end_frame: int

    @property
    def frame_count(self) -> int:
        return self.end_frame - self.start_frame + 1


@dataclass(frozen=True)
class DeliverySpec:
    dataset_id: str
    task: str
    crop_start_s: float
    crop_end_s: float
    fps: float
    segments: tuple[Segment, ...]


def _text(value: Any, name: str) -> str:
    result = " ".join(str(value).split())
    if not result:
        raise ValueError(f"{name} must not be empty")
    return result


def load_spec(path: Path) -> DeliverySpec:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if int(payload.get("schema_version", 0)) != 1:
        raise ValueError("delivery spec schema_version must be 1")
    crop = payload.get("crop", {})
    start = float(crop["start_s"])
    end = float(crop["end_s"])
    if start < 0.0 or end <= start:
        raise ValueError("crop must satisfy 0 <= start_s < end_s")
    fps = float(payload.get("fps", 30.0))
    if fps <= 0.0:
        raise ValueError("fps must be positive")
    segments = []
    for index, item in enumerate(payload.get("segments", [])):
        segment_index = int(item.get("segment_index", index))
        if segment_index != index:
            raise ValueError("segment_index values must be contiguous from zero")
        segments.append(
            Segment(
                segment_index=index,
                subtask=_text(item["subtask"], "subtask"),
                atomic_action=_text(item["atomic_action"], "atomic_action"),
                task=_text(item["task"], "task"),
                start_frame=int(item["start_frame"]),
                end_frame=int(item["end_frame"]),
            )
        )
    if not segments:
        raise ValueError("at least one action segment is required")
    return DeliverySpec(
        dataset_id=_text(payload["dataset_id"], "dataset_id"),
        task=_text(payload["task"], "task"),
        crop_start_s=start,
        crop_end_s=end,
        fps=fps,
        segments=tuple(segments),
    )


def validate_segments(segments: tuple[Segment, ...], frame_count: int) -> None:
    expected = 0
    for segment in segments:
        if segment.start_frame != expected or segment.end_frame < segment.start_frame:
            raise ValueError(f"action segment coverage breaks at frame {expected}")
        expected = segment.end_frame + 1
    if expected != frame_count:
        raise ValueError(f"action segments cover {expected}/{frame_count} frames")
