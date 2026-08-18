"""Low-cost camera freshness checks shared by preflight and active QC."""

import time
from typing import Dict, Tuple


def evaluate_cameras(node, stale_sec: float, *, now: float | None = None) -> Tuple[Dict, list[Dict]]:
    current = time.monotonic() if now is None else float(now)
    health: Dict[str, Dict] = {}
    failures: list[Dict] = []
    for camera in node.cameras:
        with node.camera_input_lock:
            samples = list(node.camera_input_times.get(camera.name, []))
        age = None if not samples else max(0.0, current - samples[-1])
        recent = [stamp for stamp in samples if current - stamp <= 2.0]
        fps = (len(recent) - 1) / max(recent[-1] - recent[0], 1e-6) if len(recent) > 1 else 0.0
        fresh = age is not None and age <= stale_sec
        health[camera.name] = {"fresh": fresh, "age_sec": age, "fps": round(fps, 2)}
        if not fresh:
            failures.append({
                "code": "camera_stale",
                "message": f"{camera.label}无新鲜图像数据",
                "details": {"camera": camera.name, "age_sec": age},
            })
    return health, failures
