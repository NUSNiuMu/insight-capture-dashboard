"""Low-cost camera freshness checks shared by preflight and active QC."""

import time
from typing import Collection, Dict, Optional, Tuple


CALIBRATION_CAMERA_NAMES = ("insight3_a", "insight3_b", "insight9_a")


def evaluate_cameras(
    node,
    stale_sec: float,
    *,
    now: float | None = None,
    camera_names: Optional[Collection[str]] = None,
) -> Tuple[Dict, list[Dict]]:
    current = time.monotonic() if now is None else float(now)
    health: Dict[str, Dict] = {}
    failures: list[Dict] = []
    for camera in node.cameras:
        if camera_names is not None and camera.name not in camera_names:
            continue
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


def evaluate_calibration_cameras(
    node,
    stale_sec: float,
    *,
    now: float | None = None,
) -> Tuple[Dict[str, Dict], list[Dict]]:
    """Check that every mapping camera has fresh image and native VIO data."""
    current = time.monotonic() if now is None else float(now)
    groups = {}
    for camera in node.cameras:
        groups.setdefault(getattr(camera, "device_name", "") or camera.name, []).append(camera)
    required_names = [pose.name for pose in getattr(node, "poses", [])] or CALIBRATION_CAMERA_NAMES
    image_health, _ = evaluate_cameras(node, stale_sec, now=current)
    health: Dict[str, Dict] = {}
    unavailable: list[Dict] = []
    liveness_times = getattr(node, "camera_liveness_times", {})

    for name in required_names:
        streams = groups.get(name, [])
        camera = streams[0] if streams else None
        images = [image_health[stream.name] for stream in streams]
        ages = [image["age_sec"] for image in images]
        image_age = max(ages) if ages and all(age is not None for age in ages) else None
        image_fresh = bool(images) and all(image["fresh"] for image in images)
        image_fps = min((image["fps"] for image in images), default=0.0)
        vio_seen = max((float(liveness_times.get(stream.name, 0.0)) for stream in streams), default=0.0)
        vio_age = None if vio_seen <= 0.0 else max(0.0, current - vio_seen)
        vio_fresh = vio_age is not None and vio_age <= stale_sec
        missing = []
        if camera is None:
            missing.append("configuration")
        else:
            if not image_fresh:
                missing.append("image")
            if not vio_fresh:
                missing.append("vio")
        entry = {
            "configured": camera is not None,
            "ready": not missing,
            "image_fresh": image_fresh,
            "image_age_sec": image_age,
            "image_fps": image_fps,
            "vio_fresh": vio_fresh,
            "vio_age_sec": vio_age,
        }
        health[name] = entry
        if missing:
            unavailable.append(
                {
                    "camera": name,
                    "label": None if camera is None else camera.label,
                    "missing": missing,
                    "image_age_sec": image_age,
                    "vio_age_sec": vio_age,
                }
            )
    return health, unavailable
