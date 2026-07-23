"""Shared runtime data models."""

from dataclasses import dataclass
from typing import Optional, Tuple

@dataclass
class PoseSpec:
    name: str
    topic: str
    color: str
    teleop_role: str
    avatar_model: Optional[str]
    avatar_scale: float
    avatar_rotation_deg_xyz: Tuple[float, float, float]
    avatar_offset_xyz: Tuple[float, float, float]


@dataclass
class CameraSpec:
    name: str
    namespace: str
    label: str
    topic: str
    camera_info_topic: str
    topic_type: str
    rotation_deg: int
    row: int
    column: int
    column_span: int
    row_span: int
    alignment_image_stream: Optional[str] = None


@dataclass
class CameraFrame:
    data: bytes
    stamp_ns: int
    received_monotonic: float
    mime_type: str
    width: int
    height: int
    version: int
    hand_overlay_pending: bool = False
