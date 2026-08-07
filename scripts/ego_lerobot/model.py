"""Stable hand-pose backend contract used by the Ego exporter."""

from __future__ import annotations

from dataclasses import dataclass
import importlib
from pathlib import Path
from typing import Any, Protocol, Sequence

import numpy as np


HAND_ORDER = ("left", "right")
HAND_INDEX = {name: index for index, name in enumerate(HAND_ORDER)}


@dataclass(frozen=True)
class HandPosePrediction:
    """One hand in the source camera coordinate system."""

    handedness: str
    confidence: float
    bbox_xyxy: np.ndarray
    keypoints_2d: np.ndarray
    keypoints_3d_camera: np.ndarray
    wrist_rotation_camera_xyzw: np.ndarray
    mano_pose_axis_angle: np.ndarray

    def __post_init__(self) -> None:
        if self.handedness not in HAND_INDEX:
            raise ValueError(f"unsupported handedness: {self.handedness!r}")
        expected = {
            "bbox_xyxy": ((4,), self.bbox_xyxy),
            "keypoints_2d": ((21, 2), self.keypoints_2d),
            "keypoints_3d_camera": ((21, 3), self.keypoints_3d_camera),
            "wrist_rotation_camera_xyzw": ((4,), self.wrist_rotation_camera_xyzw),
            "mano_pose_axis_angle": ((45,), self.mano_pose_axis_angle),
        }
        for name, (shape, value) in expected.items():
            if np.asarray(value).shape != shape:
                raise ValueError(f"{name} must have shape {shape}, got {np.asarray(value).shape}")
            if not np.all(np.isfinite(value)):
                raise ValueError(f"{name} contains NaN or Inf")
        if not np.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be finite and within [0, 1]")


class HandPoseBackend(Protocol):
    """Pluggable source-camera hand-pose estimator."""

    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    def cache_identity(self) -> dict[str, object]: ...

    def predict(self, image_bgr: np.ndarray) -> Sequence[HandPosePrediction]: ...

    def close(self) -> None: ...


def load_backend(
    backend: str,
    *,
    model_dir: Path | None = None,
    confidence: float = 0.3,
    focal_length: float,
) -> HandPoseBackend:
    """Load a built-in backend or an external ``module:factory`` plugin."""

    if backend == "wilor":
        from .wilor_backend import WiLoRBackend

        return WiLoRBackend(
            model_dir=model_dir,
            confidence=confidence,
            focal_length=focal_length,
        )
    if ":" not in backend:
        raise ValueError(
            f"unknown hand backend {backend!r}; use 'wilor' or 'module:factory'"
        )
    module_name, attribute_name = backend.split(":", 1)
    if not module_name or not attribute_name:
        raise ValueError("external hand backend must be written as module:factory")
    factory: Any = getattr(importlib.import_module(module_name), attribute_name)
    instance = factory(
        model_dir=model_dir,
        confidence=confidence,
        focal_length=focal_length,
    )
    for attribute in ("name", "version", "cache_identity", "predict", "close"):
        if not hasattr(instance, attribute):
            raise TypeError(f"hand backend is missing {attribute!r}")
    return instance
