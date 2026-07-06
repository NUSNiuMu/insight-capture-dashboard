#!/usr/bin/env python3

"""Lightweight, always-on per-section CPU-time tracker.

Not a substitute for a real profiler (py-spy, perf) -- this only times the
specific code sections wrapped in track() below. The point is that it needs
no special container capabilities (unlike py-spy, which needed SYS_PTRACE)
and its output goes straight into the same log stream
`run_dashboard.sh --logs` / `docker compose logs -f` already show, so "which
of our own hot paths is busy right now" is visible on any deployment without
attaching a profiler first.

Percentages are seconds-of-work / seconds-of-wall-clock-window, i.e. "percent
of one CPU core" -- if two cameras' work happens to overlap across threads,
their percentages both count in full and the total can exceed 100%, same as
per-process CPU% in top/htop on a multi-core box.
"""

import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Dict, Iterator

_lock = threading.Lock()
_accumulated: Dict[str, float] = defaultdict(float)
_window_start = time.monotonic()


@contextmanager
def track(category: str) -> Iterator[None]:
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        with _lock:
            _accumulated[category] += elapsed


def snapshot_and_reset() -> Dict[str, object]:
    global _window_start
    with _lock:
        now = time.monotonic()
        window_sec = now - _window_start
        totals = dict(_accumulated)
        _accumulated.clear()
        _window_start = now
    percent_of_one_core = {
        category: (seconds / window_sec * 100.0) if window_sec > 0 else 0.0
        for category, seconds in totals.items()
    }
    return {"window_sec": window_sec, "seconds": totals, "percent_of_one_core": percent_of_one_core}


def format_summary(snapshot: Dict[str, object]) -> str:
    percentages: Dict[str, float] = snapshot["percent_of_one_core"]
    window_sec = snapshot["window_sec"]
    if not percentages:
        return f"[perf] last {window_sec:.1f}s: no tracked activity"
    parts = ", ".join(
        f"{name}={pct:.1f}%" for name, pct in sorted(percentages.items(), key=lambda kv: -kv[1])
    )
    return f"[perf] last {window_sec:.1f}s (each % = share of one CPU core): {parts}"
