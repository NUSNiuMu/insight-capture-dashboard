"""Small web-facing helpers shared by route modules and the ROS node."""

import os
from pathlib import Path
from typing import Dict

def _read_tum_points(path: Path, max_points: int = 2000) -> list:
    """Read a TUM trajectory file and return a downsampled list of [x, y, z] points."""
    if not path.exists():
        return []
    points = []
    try:
        with path.open("r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) >= 4:
                    points.append([float(parts[1]), float(parts[2]), float(parts[3])])
    except Exception:
        return []
    if len(points) <= max_points:
        return points
    step = len(points) / max_points
    return [points[int(i * step)] for i in range(max_points)]


def read_system_load() -> Dict[str, object]:
    """Return host load and container CPU-quota utilization."""
    load_1min = load_5min = None
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        load_1min, load_5min = float(parts[0]), float(parts[1])
    except (OSError, ValueError, IndexError):
        pass

    cpu_quota_cores = None
    try:
        with open("/sys/fs/cgroup/cpu.max") as f:
            quota_str, period_str = f.read().split()
        if quota_str != "max":
            cpu_quota_cores = int(quota_str) / int(period_str)
    except (OSError, ValueError):
        pass

    cpu_count = os.cpu_count() or 1
    budget = cpu_quota_cores or cpu_count
    return {
        "load_1min": load_1min,
        "load_5min": load_5min,
        "cpu_count": cpu_count,
        "cpu_quota_cores": cpu_quota_cores,
        "load_ratio": (load_1min / budget) if load_1min is not None else None,
    }


def bagplay_topic(topic: str) -> str:
    """Return the isolated playback topic for a live ROS topic."""
    return f"/bagplay{topic}"
