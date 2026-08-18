"""Persistent settings shared by the dashboard and Insight3 localizer."""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path


DEFAULT_GRIPPER_MASK_HEIGHT_RATIO = 0.2
SETTINGS_SECTION = "insight3_global_localizer"
GRIPPER_MASK_HEIGHT_RATIO_KEY = "gripper_mask_height_ratio"


def validate_gripper_mask_height_ratio(value: object) -> float:
    """Return a finite mask ratio in [0, 1), rejecting booleans."""

    if isinstance(value, bool):
        raise ValueError("gripper mask height ratio must be a number in [0, 1)")
    try:
        ratio = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "gripper mask height ratio must be a number in [0, 1)"
        ) from exc
    if not math.isfinite(ratio) or not 0.0 <= ratio < 1.0:
        raise ValueError("gripper mask height ratio must be a number in [0, 1)")
    return ratio


def load_gripper_mask_height_ratio(
    config_path: Path,
    *,
    default: float = DEFAULT_GRIPPER_MASK_HEIGHT_RATIO,
) -> float:
    """Load the Insight3 mask ratio from the runtime config."""

    fallback = validate_gripper_mask_height_ratio(default)
    path = Path(config_path)
    if not path.is_file():
        return fallback
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("post-processing config must be a JSON object")
    section = payload.get(SETTINGS_SECTION, {})
    if not isinstance(section, dict):
        raise ValueError(f"'{SETTINGS_SECTION}' must be a JSON object")
    return validate_gripper_mask_height_ratio(
        section.get(GRIPPER_MASK_HEIGHT_RATIO_KEY, fallback)
    )


def save_gripper_mask_height_ratio(config_path: Path, value: object) -> float:
    """Atomically persist the Insight3 mask ratio in the runtime config."""

    ratio = validate_gripper_mask_height_ratio(value)
    path = Path(config_path)
    payload = {}
    existing_stat = None
    if path.is_file():
        existing_stat = path.stat()
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("post-processing config must be a JSON object")
    section = payload.get(SETTINGS_SECTION, {})
    if not isinstance(section, dict):
        raise ValueError(f"'{SETTINGS_SECTION}' must be a JSON object")
    payload[SETTINGS_SECTION] = {
        **section,
        GRIPPER_MASK_HEIGHT_RATIO_KEY: ratio,
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(payload, temporary, indent=2)
            temporary.write("\n")
            temporary.flush()
            if existing_stat is not None:
                os.fchmod(temporary.fileno(), existing_stat.st_mode & 0o777)
                try:
                    os.fchown(
                        temporary.fileno(), existing_stat.st_uid, existing_stat.st_gid
                    )
                except PermissionError:
                    pass
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return ratio
