"""Persistent settings shared by the dashboard and Insight3 localizer."""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path


SETTINGS_SECTION = "localization"
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


def _load_config(config_path: Path) -> dict:
    payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("runtime config must be a JSON object")
    if not isinstance(payload[SETTINGS_SECTION], dict):
        raise ValueError(f"'{SETTINGS_SECTION}' must be a JSON object")
    return payload


def load_gripper_mask_height_ratio(config_path: Path) -> float:
    """Read the required localization mask setting."""
    return validate_gripper_mask_height_ratio(_load_config(config_path)[SETTINGS_SECTION][GRIPPER_MASK_HEIGHT_RATIO_KEY])


def save_gripper_mask_height_ratio(config_path: Path, value: object) -> float:
    """Atomically persist the Insight3 mask ratio in the runtime config."""

    ratio = validate_gripper_mask_height_ratio(value)
    path = Path(config_path)
    existing_stat = path.stat()
    payload = _load_config(path)
    payload[SETTINGS_SECTION][GRIPPER_MASK_HEIGHT_RATIO_KEY] = ratio

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
            os.fchmod(temporary.fileno(), existing_stat.st_mode & 0o777)
            if (existing_stat.st_uid, existing_stat.st_gid) != (os.geteuid(), os.getegid()):
                os.fchown(temporary.fileno(), existing_stat.st_uid, existing_stat.st_gid)
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return ratio
