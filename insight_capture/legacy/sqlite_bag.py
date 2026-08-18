"""Legacy SQLite rosbag compatibility; new captures use one native MCAP."""

from __future__ import annotations

import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from insight_capture.postprocess.bags.integrity import nominal_for

try:
    import yaml
except Exception:  # pragma: no cover - caller falls back to ros2 bag convert
    yaml = None


MAX_TRIM_NS = 2_000_000_000
MAX_ATTACHED_PARTS = 9


class FastMergeUnavailable(RuntimeError):
    """The input layout is not safe for the direct SQLite merge path."""


def _single_database(part: Path) -> Path:
    databases = sorted(part.glob("*.db3"))
    if len(databases) != 1:
        raise FastMergeUnavailable(
            f"{part.name} has {len(databases)} SQLite files; expected exactly one"
        )
    return databases[0]


def _combine_stat(
    current: Tuple[int, Optional[int], Optional[int]],
    incoming: Tuple[int, Optional[int], Optional[int]],
) -> Tuple[int, Optional[int], Optional[int]]:
    count = current[0] + incoming[0]
    starts = [value for value in (current[1], incoming[1]) if value is not None]
    ends = [value for value in (current[2], incoming[2]) if value is not None]
    return count, min(starts) if starts else None, max(ends) if ends else None


def _inspect_parts(part_bags: Sequence[Path]) -> Dict[str, object]:
    if not part_bags:
        raise FastMergeUnavailable("No rosbag parts were provided")
    if len(part_bags) > MAX_ATTACHED_PARTS:
        raise FastMergeUnavailable(
            f"{len(part_bags)} parts exceed SQLite's safe attach limit of {MAX_ATTACHED_PARTS}"
        )

    parts: List[Dict[str, object]] = []
    topics_by_name: Dict[str, Dict[str, object]] = {}
    schema_row: Optional[Tuple[int, str]] = None
    frame_first_by_topic: Dict[str, int] = {}
    source_bytes = 0

    for part in part_bags:
        db_path = _single_database(part)
        source_bytes += db_path.stat().st_size
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            current_schema = conn.execute(
                "SELECT schema_version, ros_distro FROM schema"
            ).fetchone()
            if current_schema is None:
                raise FastMergeUnavailable(f"{part.name} has no rosbag schema row")
            normalized_schema = (int(current_schema[0]), str(current_schema[1]))
            if schema_row is None:
                schema_row = normalized_schema
            elif schema_row != normalized_schema:
                raise FastMergeUnavailable(
                    f"{part.name} uses schema {normalized_schema}, expected {schema_row}"
                )

            topic_rows = conn.execute(
                "SELECT id, name, type, serialization_format, offered_qos_profiles "
                "FROM topics ORDER BY id"
            ).fetchall()
            aggregate_rows = {
                int(topic_id): (int(count), int(first), int(last))
                for topic_id, count, first, last in conn.execute(
                    "SELECT topic_id, COUNT(*), MIN(timestamp), MAX(timestamp) "
                    "FROM messages GROUP BY topic_id"
                )
            }
            part_topics: List[Dict[str, object]] = []
            for topic_id, name, topic_type, serialization_format, qos_profiles in topic_rows:
                descriptor = {
                    "name": str(name),
                    "type": str(topic_type),
                    "serialization_format": str(serialization_format),
                    "offered_qos_profiles": str(qos_profiles),
                }
                existing = topics_by_name.get(descriptor["name"])
                if existing is None:
                    topics_by_name[descriptor["name"]] = descriptor
                elif existing != descriptor:
                    raise FastMergeUnavailable(
                        f"Conflicting metadata for topic {descriptor['name']}"
                    )
                stat = aggregate_rows.get(int(topic_id), (0, None, None))
                if nominal_for(descriptor["name"]) is not None and stat[1] is not None:
                    first = int(stat[1])
                    previous = frame_first_by_topic.get(descriptor["name"])
                    frame_first_by_topic[descriptor["name"]] = (
                        first if previous is None else min(previous, first)
                    )
                part_topics.append({"source_id": int(topic_id), "descriptor": descriptor, "stat": stat})
            parts.append({"path": part, "db_path": db_path, "topics": part_topics})
        finally:
            conn.close()

    first_values = list(frame_first_by_topic.values())
    trim = {"trimmed_ns": 0}
    trim_point_ns: Optional[int] = None
    if first_values:
        sync_point_ns = max(first_values)
        old_min_ns = min(first_values)
        if sync_point_ns > old_min_ns:
            trim_point_ns = min(sync_point_ns, old_min_ns + MAX_TRIM_NS)
            capped = trim_point_ns < sync_point_ns
            trim = {
                "trimmed_ns": int(trim_point_ns - old_min_ns),
                "sync_point_ns": int(sync_point_ns),
                "capped": capped,
                "residual_skew_ns": int(sync_point_ns - trim_point_ns) if capped else 0,
            }

    expected_stats: Dict[str, Tuple[int, Optional[int], Optional[int]]] = {
        name: (0, None, None) for name in topics_by_name
    }
    for part in parts:
        conn = sqlite3.connect(f"file:{part['db_path']}?mode=ro", uri=True)
        try:
            for topic in part["topics"]:
                descriptor = topic["descriptor"]
                stat = topic["stat"]
                if trim_point_ns is not None and nominal_for(descriptor["name"]) is not None:
                    row = conn.execute(
                        "SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM messages "
                        "WHERE topic_id = ? AND timestamp >= ?",
                        (topic["source_id"], trim_point_ns),
                    ).fetchone()
                    stat = (
                        int(row[0]),
                        int(row[1]) if row[1] is not None else None,
                        int(row[2]) if row[2] is not None else None,
                    )
                expected_stats[descriptor["name"]] = _combine_stat(
                    expected_stats[descriptor["name"]], stat
                )
        finally:
            conn.close()

    return {
        "parts": parts,
        "topics_by_name": topics_by_name,
        "schema_row": schema_row,
        "trim": trim,
        "trim_point_ns": trim_point_ns,
        "expected_stats": expected_stats,
        "source_bytes": source_bytes,
    }


def _create_output_database(plan: Dict[str, object], output_db: Path) -> Dict[str, float]:
    started = time.perf_counter()
    conn = sqlite3.connect(str(output_db))
    try:
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA cache_size = -131072")
        conn.execute("PRAGMA locking_mode = EXCLUSIVE")
        conn.execute("CREATE TABLE schema(schema_version INTEGER PRIMARY KEY,ros_distro TEXT NOT NULL)")
        conn.execute(
            "CREATE TABLE metadata(id INTEGER PRIMARY KEY,metadata_version INTEGER NOT NULL,metadata TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE topics(id INTEGER PRIMARY KEY,name TEXT NOT NULL,type TEXT NOT NULL,"
            "serialization_format TEXT NOT NULL,offered_qos_profiles TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE messages(id INTEGER PRIMARY KEY,topic_id INTEGER NOT NULL,"
            "timestamp INTEGER NOT NULL,data BLOB NOT NULL)"
        )
        conn.execute("INSERT INTO schema(schema_version, ros_distro) VALUES (?, ?)", plan["schema_row"])

        output_topic_ids: Dict[str, int] = {}
        for output_id, name in enumerate(sorted(plan["topics_by_name"]), start=1):
            descriptor = plan["topics_by_name"][name]
            output_topic_ids[name] = output_id
            conn.execute(
                "INSERT INTO topics(id, name, type, serialization_format, offered_qos_profiles) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    output_id,
                    descriptor["name"],
                    descriptor["type"],
                    descriptor["serialization_format"],
                    descriptor["offered_qos_profiles"],
                ),
            )
        conn.commit()

        for index, part in enumerate(plan["parts"]):
            alias = f"part_{index}"
            conn.execute(f"ATTACH DATABASE ? AS {alias}", (str(part["db_path"]),))

        insert_started = time.perf_counter()
        conn.execute("BEGIN IMMEDIATE")
        for index, part in enumerate(plan["parts"]):
            alias = f"part_{index}"
            for topic in part["topics"]:
                descriptor = topic["descriptor"]
                params: Tuple[object, ...] = (
                    output_topic_ids[descriptor["name"]],
                    topic["source_id"],
                )
                predicate = "topic_id = ?"
                if plan["trim_point_ns"] is not None and nominal_for(descriptor["name"]) is not None:
                    predicate += " AND timestamp >= ?"
                    params += (plan["trim_point_ns"],)
                conn.execute(
                    f"INSERT INTO messages(topic_id, timestamp, data) "
                    f"SELECT ?, timestamp, data FROM {alias}.messages WHERE {predicate}",
                    params,
                )
        conn.commit()
        insert_sec = time.perf_counter() - insert_started

        index_started = time.perf_counter()
        conn.execute("CREATE INDEX timestamp_idx ON messages (timestamp ASC)")
        conn.commit()
        index_sec = time.perf_counter() - index_started
    finally:
        conn.close()
    return {
        "sqlite_setup_and_insert_sec": round(time.perf_counter() - started - index_sec, 3),
        "sqlite_insert_sec": round(insert_sec, 3),
        "sqlite_index_sec": round(index_sec, 3),
    }


def _write_metadata(plan: Dict[str, object], output_path: Path, output_db: Path) -> None:
    if yaml is None:
        raise FastMergeUnavailable("PyYAML is unavailable")
    expected_stats = plan["expected_stats"]
    frame_starts = [
        stat[1]
        for name, stat in expected_stats.items()
        if nominal_for(name) is not None and stat[1] is not None
    ]
    all_starts = [stat[1] for stat in expected_stats.values() if stat[1] is not None]
    all_ends = [stat[2] for stat in expected_stats.values() if stat[2] is not None]
    if not all_starts or not all_ends:
        raise FastMergeUnavailable("Merged rosbag contains no messages")
    starting_time = int(min(frame_starts) if frame_starts else min(all_starts))
    ending_time = int(max(all_ends))
    message_count = sum(stat[0] for stat in expected_stats.values())

    topics = []
    for name in sorted(plan["topics_by_name"]):
        descriptor = plan["topics_by_name"][name]
        topics.append(
            {
                "topic_metadata": dict(descriptor),
                "message_count": int(expected_stats[name][0]),
            }
        )
    relative_path = output_db.name
    info = {
        "version": 5,
        "storage_identifier": "sqlite3",
        "duration": {"nanoseconds": max(0, ending_time - starting_time)},
        "starting_time": {"nanoseconds_since_epoch": starting_time},
        "message_count": int(message_count),
        "topics_with_message_count": topics,
        "compression_format": "",
        "compression_mode": "",
        "relative_file_paths": [relative_path],
        "files": [
            {
                "path": relative_path,
                "starting_time": {"nanoseconds_since_epoch": starting_time},
                "duration": {"nanoseconds": max(0, ending_time - starting_time)},
                "message_count": int(message_count),
            }
        ],
    }
    (output_path / "metadata.yaml").write_text(
        yaml.safe_dump(
            {"rosbag2_bagfile_information": info},
            sort_keys=False,
            default_flow_style=False,
            width=1_000_000,
        ),
        encoding="utf-8",
    )


def _validate_output(plan: Dict[str, object], output_db: Path) -> None:
    conn = sqlite3.connect(f"file:{output_db}?mode=ro", uri=True)
    try:
        quick_check = conn.execute("PRAGMA quick_check").fetchone()
        if quick_check is None or quick_check[0] != "ok":
            raise RuntimeError("Merged database failed SQLite quick_check")
        topic_names = {
            int(topic_id): str(name)
            for topic_id, name in conn.execute("SELECT id, name FROM topics")
        }
        actual = {name: (0, None, None) for name in topic_names.values()}
        for topic_id, count, first, last in conn.execute(
            "SELECT topic_id, COUNT(*), MIN(timestamp), MAX(timestamp) "
            "FROM messages GROUP BY topic_id"
        ):
            actual[topic_names[int(topic_id)]] = (int(count), int(first), int(last))
        if actual != plan["expected_stats"]:
            raise RuntimeError("Merged topic counts or timestamp bounds do not match source parts")
    finally:
        conn.close()


def merge_sqlite_parts(part_bags: Sequence[Path], output_path: Path) -> Dict[str, object]:
    """Merge rosbag SQLite files in bulk and atomically publish a validated output."""
    total_started = time.perf_counter()
    inspect_started = time.perf_counter()
    plan = _inspect_parts(part_bags)
    inspect_sec = time.perf_counter() - inspect_started

    temp_path = output_path.parent / f".{output_path.name}.sqlite_merge_tmp"
    shutil.rmtree(temp_path, ignore_errors=True)
    temp_path.mkdir(parents=True)
    output_db = temp_path / f"{output_path.name}_0.db3"
    try:
        timings = _create_output_database(plan, output_db)
        metadata_started = time.perf_counter()
        _write_metadata(plan, temp_path, output_db)
        metadata_sec = time.perf_counter() - metadata_started
        validation_started = time.perf_counter()
        _validate_output(plan, output_db)
        validation_sec = time.perf_counter() - validation_started
        output_bytes = output_db.stat().st_size
        if output_path.exists():
            shutil.rmtree(output_path)
        os.replace(temp_path, output_path)
    except Exception:
        shutil.rmtree(temp_path, ignore_errors=True)
        raise

    timings.update(
        {
            "inspect_sec": round(inspect_sec, 3),
            "metadata_sec": round(metadata_sec, 3),
            "validation_sec": round(validation_sec, 3),
            "total_sec": round(time.perf_counter() - total_started, 3),
        }
    )
    return {
        "method": "sqlite_bulk",
        "trim_applied": True,
        "trim": plan["trim"],
        "source_bytes": int(plan["source_bytes"]),
        "output_bytes": output_bytes,
        "timings": timings,
    }
