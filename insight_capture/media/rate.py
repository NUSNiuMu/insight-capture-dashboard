"""Deadline-based rate selection for latest-frame media paths."""

from __future__ import annotations

from typing import Tuple


def observed_frame_rate(timestamps, now: float, window_sec: float = 2.0) -> float:
    """Count arrivals over wall time, including pauses after the last frame."""
    if not timestamps:
        return 0.0
    cutoff = now - window_sec
    count = sum(cutoff < stamp <= now for stamp in timestamps)
    span = min(window_sec, max(0.0, now - timestamps[0]))
    if timestamps[0] > cutoff:
        count = max(0, count - 1)
    return count / span if span > 0.0 else 0.0


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
