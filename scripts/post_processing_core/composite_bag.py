"""Discover and summarize single or composite rosbag2 sessions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Set, Tuple

try:
    import yaml
except Exception:  # pragma: no cover - callers degrade to an empty catalog
    yaml = None


MANIFEST_NAME = "recording_manifest.json"
COMPOSITE_FORMAT = "composite_rosbag2"


def read_metadata(bag_path: Path) -> Dict[str, object]:
    metadata_path = bag_path / "metadata.yaml"
    if yaml is None or not metadata_path.is_file():
        return {}
    try:
        payload = yaml.safe_load(metadata_path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, TypeError):
        return {}
    info = payload.get("rosbag2_bagfile_information", {})
    return info if isinstance(info, dict) else {}


def session_parts(bag_path: Path) -> List[Path]:
    """Return rosbag2 directories in deterministic session order."""
    bag_path = Path(bag_path)
    if (bag_path / "metadata.yaml").is_file():
        return [bag_path]
    if list(bag_path.glob("*.db3")) or list(bag_path.glob("*.mcap")):
        return [bag_path]
    manifest_path = bag_path / MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    if manifest.get("format") != COMPOSITE_FORMAT:
        return []
    parts = []
    for entry in manifest.get("parts") or []:
        relative = entry.get("path") if isinstance(entry, dict) else entry
        if not isinstance(relative, str) or Path(relative).is_absolute():
            continue
        part = (bag_path / relative).resolve()
        if part.is_relative_to(bag_path.resolve()) and (part / "metadata.yaml").is_file():
            parts.append(part)
    return parts


def aggregate_metadata(bag_path: Path) -> Dict[str, object]:
    """Return standard-shaped metadata aggregated across session parts."""
    parts = session_parts(bag_path)
    if not parts:
        return {}
    starts: List[int] = []
    ends: List[int] = []
    topics: Dict[str, Dict[str, object]] = {}
    storage_ids = set()
    for part in parts:
        info = read_metadata(part)
        if not info:
            continue
        storage_id = str(info.get("storage_identifier") or "")
        if storage_id:
            storage_ids.add(storage_id)
        start_ns = int((info.get("starting_time") or {}).get("nanoseconds_since_epoch", 0) or 0)
        duration_ns = int((info.get("duration") or {}).get("nanoseconds", 0) or 0)
        if start_ns:
            starts.append(start_ns)
            ends.append(start_ns + max(duration_ns, 0))
        for entry in info.get("topics_with_message_count") or []:
            if not isinstance(entry, dict):
                continue
            descriptor = entry.get("topic_metadata") or {}
            name = str(descriptor.get("name") or "")
            if not name:
                continue
            current = topics.setdefault(
                name,
                {"topic_metadata": dict(descriptor), "message_count": 0},
            )
            current["message_count"] = int(current["message_count"]) + int(
                entry.get("message_count", 0) or 0
            )
    if not topics:
        return {}
    start_ns = min(starts) if starts else 0
    end_ns = max(ends) if ends else start_ns
    return {
        "storage_identifier": (
            next(iter(storage_ids)) if len(storage_ids) == 1 else COMPOSITE_FORMAT
        ),
        "duration": {"nanoseconds": max(0, end_ns - start_ns)},
        "starting_time": {"nanoseconds_since_epoch": start_ns},
        "message_count": sum(int(entry["message_count"]) for entry in topics.values()),
        "topics_with_message_count": [topics[name] for name in sorted(topics)],
        "parts": [str(part.relative_to(Path(bag_path))) for part in parts],
    }


def recorded_topics(bag_path: Path) -> List[str]:
    metadata = aggregate_metadata(bag_path)
    return [
        str((entry.get("topic_metadata") or {}).get("name"))
        for entry in metadata.get("topics_with_message_count") or []
        if (entry.get("topic_metadata") or {}).get("name")
    ]


def topic_types(bag_path: Path) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for part in session_parts(bag_path):
        info = read_metadata(part)
        for entry in info.get("topics_with_message_count") or []:
            descriptor = entry.get("topic_metadata") or {}
            name = str(descriptor.get("name") or "")
            message_type = str(descriptor.get("type") or "")
            if not name or not message_type:
                continue
            previous = result.setdefault(name, message_type)
            if previous != message_type:
                raise ValueError(f"topic {name} changes type across bag parts")
        if not info:
            import sqlite3

            for db_path in sorted(part.glob("*.db3")):
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                try:
                    for name, message_type in conn.execute("SELECT name, type FROM topics"):
                        previous = result.setdefault(str(name), str(message_type))
                        if previous != str(message_type):
                            raise ValueError(f"topic {name} changes type across bag files")
                finally:
                    conn.close()
    return result


def iter_messages(
    bag_path: Path, topics: Iterable[str]
) -> Iterator[Tuple[str, bytes, int]]:
    """Yield selected serialized messages from SQLite or MCAP parts."""
    requested: Set[str] = {str(topic) for topic in topics}
    if not requested:
        return
    for part in session_parts(bag_path):
        info = read_metadata(part)
        part_topics = {
            str((entry.get("topic_metadata") or {}).get("name") or "")
            for entry in info.get("topics_with_message_count") or []
        }
        if part_topics and not requested.intersection(part_topics):
            continue
        db_files = sorted(part.glob("*.db3"))
        if db_files:
            import sqlite3

            for db_path in db_files:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                try:
                    topic_ids = [
                        int(topic_id)
                        for topic_id, name in conn.execute("SELECT id, name FROM topics")
                        if str(name) in requested
                    ]
                    if not topic_ids:
                        continue
                    placeholders = ",".join("?" for _ in topic_ids)
                    for name, data, timestamp in conn.execute(
                        "SELECT t.name, m.data, m.timestamp FROM messages m "
                        "JOIN topics t ON t.id = m.topic_id "
                        f"WHERE m.topic_id IN ({placeholders}) ORDER BY m.timestamp, m.id",
                        topic_ids,
                    ):
                        yield str(name), bytes(data), int(timestamp)
                finally:
                    conn.close()
            continue

        import rosbag2_py

        reader = rosbag2_py.SequentialReader()
        reader.open(
            rosbag2_py.StorageOptions(
                uri=str(part), storage_id=str(info.get("storage_identifier") or "")
            ),
            rosbag2_py.ConverterOptions("", ""),
        )
        while reader.has_next():
            topic, data, timestamp = reader.read_next()
            if str(topic) in requested:
                yield str(topic), bytes(data), int(timestamp)


def source_files(bag_path: Path) -> List[Path]:
    """Return stable source files used to invalidate derived review caches."""
    bag_path = Path(bag_path)
    files = []
    manifest = bag_path / MANIFEST_NAME
    if manifest.is_file():
        files.append(manifest)
    for part in session_parts(bag_path):
        metadata = part / "metadata.yaml"
        if metadata.is_file():
            files.append(metadata)
        files.extend(
            item for item in sorted(part.iterdir())
            if item.is_file() and item.suffix in {".db3", ".mcap"}
        )
    return files
