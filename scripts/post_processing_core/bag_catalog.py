"""Read-only rosbag catalog and result badge helpers."""

import json
import os
from pathlib import Path
from typing import Dict, List, Optional

try:
    import yaml
except Exception:  # pragma: no cover - metadata parsing degrades gracefully
    yaml = None

def _format_bytes(size_bytes: int) -> str:
    value = float(max(int(size_bytes), 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0


def _directory_size_bytes(path: Path) -> int:
    total = 0
    stack = [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            total += entry.stat().st_size
                        elif entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _result_exists(results_root: Path, category: str, bag_name: str) -> bool:
    candidates = [
        results_root / category / f"{bag_name}.json",
        results_root / category / bag_name,
        results_root / f"{bag_name}_{category}.json",
    ]
    return any(candidate.exists() for candidate in candidates)


def _read_bag_metadata(metadata_path: Path) -> Dict[str, object]:
    if yaml is None or not metadata_path.exists():
        return {}
    try:
        # CSafeLoader (libyaml) parses ~10x faster than the pure-Python
        # SafeLoader; the bag list re-parses every metadata.yaml per request,
        # which dominated /api/rosbags latency (~34ms vs ~3ms per bag on
        # Jetson). Fall back if PyYAML was built without libyaml.
        loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
        payload = yaml.load(metadata_path.read_text(encoding="utf-8"), Loader=loader) or {}
    except Exception:
        return {}
    info = payload.get("rosbag2_bagfile_information", {})
    return info if isinstance(info, dict) else {}


def list_rosbags(rosbag_root: Path, results_root: Path) -> List[Dict[str, object]]:
    if not rosbag_root.exists():
        return []
    entries: List[Dict[str, object]] = []
    for bag_dir in sorted(rosbag_root.iterdir(), key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True):
        if not bag_dir.is_dir():
            continue
        metadata_path = bag_dir / "metadata.yaml"
        if not metadata_path.exists():
            continue
        metadata = _read_bag_metadata(metadata_path)
        duration_ns = int((metadata.get("duration") or {}).get("nanoseconds", 0) or 0)
        message_count = int(metadata.get("message_count", 0) or 0)
        topics = metadata.get("topics_with_message_count") or []
        size_bytes = _directory_size_bytes(bag_dir)
        labeled = (
            _result_exists(results_root, "labels", bag_dir.name)
            or _result_exists(results_root, "label", bag_dir.name)
            or _result_exists(results_root, "labeled", bag_dir.name)
        )
        scored = _result_exists(results_root, "scores", bag_dir.name) or _result_exists(results_root, "scoring", bag_dir.name)
        optimized = _result_exists(results_root, "optimized", bag_dir.name) or _result_exists(results_root, "optimization", bag_dir.name)
        # Unlike labeled/scored, mere file existence isn't enough here: the
        # persisted report (written by /api/integrity/run) says pass or
        # fail, and the badge must show which. None = never checked.
        integrity: Optional[bool] = None
        integrity_path = results_root / "integrity" / f"{bag_dir.name}.json"
        if integrity_path.exists():
            try:
                integrity = bool(json.loads(integrity_path.read_text()).get("ok"))
            except (OSError, ValueError, AttributeError):
                integrity = None
        entries.append(
            {
                "name": bag_dir.name,
                "path": str(bag_dir),
                "size_bytes": size_bytes,
                "size_label": _format_bytes(size_bytes),
                "duration_s": duration_ns / 1_000_000_000.0,
                "message_count": message_count,
                "topic_count": len(topics) if isinstance(topics, list) else 0,
                "modified_at_epoch_s": bag_dir.stat().st_mtime,
                "labeled": labeled,
                "scored": scored,
                "optimized": optimized,
                "integrity": integrity,
                "label": (
                    f"{'labeled' if labeled else 'unlabeled'} / "
                    f"{'scored' if scored else 'unscored'} / "
                    f"{'optimized' if optimized else 'not optimized'}"
                ),
            }
        )
    return entries
