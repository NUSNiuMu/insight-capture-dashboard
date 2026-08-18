"""Content-keyed stage cache and hard-link materialization helpers."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile


CACHE_SCHEMA_VERSION = 1


def canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def bag_fingerprint(path: Path) -> dict[str, object]:
    """Fingerprint bag identity without hashing multi-gigabyte SQLite payloads."""

    path = path.resolve()
    metadata = path / "metadata.yaml"
    if not metadata.is_file():
        raise FileNotFoundError(f"rosbag metadata is missing: {metadata}")
    files = []
    for item in sorted(path.iterdir()):
        if not item.is_file():
            continue
        stat = item.stat()
        files.append(
            {
                "name": item.name,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    return {
        "path_name": path.name,
        "metadata_sha256": hashlib.sha256(metadata.read_bytes()).hexdigest(),
        "files": files,
    }


def read_manifest(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if payload.get("cache_schema_version") != CACHE_SCHEMA_VERSION:
        return None
    return payload


def write_manifest(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {"cache_schema_version": CACHE_SCHEMA_VERSION, **payload}
    temporary = path.with_suffix(path.suffix + ".pending")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def link_or_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def materialize_tree(source: Path, target: Path) -> None:
    """Atomically clone a cached dataset, hard-linking large immutable files."""

    if target.exists():
        raise FileExistsError(f"refusing to overwrite output: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ego_materialize_", dir=target.parent) as temp:
        staged = Path(temp) / "dataset"
        for item in source.rglob("*"):
            relative = item.relative_to(source)
            destination = staged / relative
            if item.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            elif item.is_file():
                if item.suffix.lower() == ".mp4":
                    link_or_copy(item, destination)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, destination)
        staged.replace(target)
