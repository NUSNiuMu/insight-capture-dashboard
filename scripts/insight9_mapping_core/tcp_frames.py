"""Load calibrated camera-center to TCP frame transforms."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np


@dataclass(frozen=True)
class TcpFrameCalibration:
    """A static ``T_camera_center_tcp`` transform from device configuration."""

    camera_name: str
    parent_frame_id: str
    child_frame_id: str
    translation_m: tuple[float, float, float]
    rotation_xyzw: tuple[float, float, float, float]


def _finite_vector(value, size: int, label: str) -> np.ndarray:
    vector = np.asarray(value, dtype=np.float64)
    if vector.shape != (size,) or not np.isfinite(vector).all():
        raise ValueError(f"{label} must contain {size} finite values")
    return vector


def load_tcp_frame_calibrations(
    config_path: Path, camera_names: Iterable[str]
) -> dict[str, TcpFrameCalibration]:
    """Return complete TCP calibrations for the requested cameras."""

    requested = set(camera_names)
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    calibrations: dict[str, TcpFrameCalibration] = {}
    child_frames: set[str] = set()
    for camera in config.get("cameras", []):
        name = str(camera.get("name", ""))
        if name not in requested:
            continue
        frame_id = camera.get("tcp_frame_id")
        transform = camera.get("camera_center_to_tcp")
        if transform is None:
            continue
        if not isinstance(frame_id, str) or not frame_id:
            raise ValueError(f"{name} TCP calibration requires tcp_frame_id")
        if frame_id in child_frames:
            raise ValueError(f"duplicate TCP frame_id: {frame_id}")
        translation = _finite_vector(
            transform.get("translation_m"), 3, f"{name} TCP translation_m"
        )
        rotation = _finite_vector(
            transform.get("rotation_xyzw"), 4, f"{name} TCP rotation_xyzw"
        )
        norm = float(np.linalg.norm(rotation))
        if norm < 1e-12:
            raise ValueError(f"{name} TCP rotation_xyzw must be non-zero")
        rotation /= norm
        calibrations[name] = TcpFrameCalibration(
            camera_name=name,
            parent_frame_id=f"{name}_global_camera_center",
            child_frame_id=frame_id,
            translation_m=tuple(float(value) for value in translation),
            rotation_xyzw=tuple(float(value) for value in rotation),
        )
        child_frames.add(frame_id)
    return calibrations
