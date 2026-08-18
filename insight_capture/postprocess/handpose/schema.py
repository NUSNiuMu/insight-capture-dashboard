"""Shared hand-pose result helpers."""

from __future__ import annotations

from pathlib import Path


DEFAULT_IMAGE_TOPIC = "/insight9_a/camera/color/image_rect_raw/compressed"
METHODS = ("wilor",)


def safe_child(root: Path, name: str) -> Path:
    """Resolve one direct child without allowing traversal or absolute paths."""
    if not name or name in {".", ".."} or Path(name).name != name:
        raise ValueError("Invalid name")
    candidate = (root / name).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("Path is outside the configured root") from exc
    return candidate
