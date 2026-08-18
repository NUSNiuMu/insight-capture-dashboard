#!/usr/bin/env python3

"""Track selected hot paths as percentages of one CPU core."""

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
