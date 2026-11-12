"""Pause-segmentation (静止分段) parameters, editable from the review page.

Stored once for all recordings in ``<output>/_segment_settings.json``; both
the 静止分段 button and pre-annotation read them, so the two always split
the same way.
"""

import json
from pathlib import Path

from . import dataset
from .capture import write_json

FILE = "_segment_settings.json"
# key -> (label, unit, default, minimum, maximum)
FIELDS = {
    "idle_duration_s": ("最短停顿时长", "s", dataset.DEFAULT_IDLE_DURATION_S, 0.1, 30.0),
    "idle_linear_speed_m_s": ("静止线速度上限", "m/s", dataset.DEFAULT_IDLE_LINEAR_SPEED_M_S, 0.001, 0.5),
    "idle_angular_speed_deg_s": ("静止角速度上限", "°/s", dataset.DEFAULT_IDLE_ANGULAR_SPEED_DEG_S, 0.5, 180.0),
    "idle_gripper_speed_m_s": ("静止开合速度上限", "m/s", dataset.DEFAULT_IDLE_GRIPPER_SPEED_M_S, 0.0005, 0.2),
    "idle_gap_tolerance_s": ("停顿内允许的短暂运动", "s", dataset.DEFAULT_IDLE_GAP_TOLERANCE_S, 0.0, 2.0),
    "minimum_duration_s": ("最短片段时长", "s", dataset.DEFAULT_MINIMUM_SEGMENT_DURATION_S, 0.1, 60.0),
}


def defaults():
    return {key: spec[2] for key, spec in FIELDS.items()}


def load(root):
    path = Path(root) / FILE
    stored = json.loads(path.read_text()) if path.is_file() else {}
    return {**defaults(), **{k: float(v) for k, v in stored.items() if k in FIELDS}}


def save(root, values):
    if values.get("reset"):
        result = defaults()
    else:
        result = load(root)
        for key, value in values.items():
            if key not in FIELDS:
                continue
            label, unit, _default, low, high = FIELDS[key]
            number = float(value)
            if not low <= number <= high:
                raise ValueError(f"{label}须在 {low:g}–{high:g} {unit} 之间")
            result[key] = number
    write_json(Path(root) / FILE, result)
    return result


def describe(root):
    current = load(root)
    return {
        "values": current,
        "fields": [
            {"key": k, "label": s[0], "unit": s[1], "default": s[2], "min": s[3], "max": s[4], "value": current[k]}
            for k, s in FIELDS.items()
        ],
    }
