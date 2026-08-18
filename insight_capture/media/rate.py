"""Deadline-based rate selection for latest-frame media paths."""

from __future__ import annotations

from typing import Tuple


def select_frame(
    now: float, next_frame_at: float, target_fps: int
) -> Tuple[bool, float]:
    """Return whether to keep this frame and the next selection deadline."""
    fps = max(1, int(target_fps))
    if now < next_frame_at:
        return False, next_frame_at
    period = 1.0 / fps
    if next_frame_at <= 0.0 or now - next_frame_at >= period:
        next_frame_at = now
    return True, next_frame_at + period
