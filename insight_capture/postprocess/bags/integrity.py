#!/usr/bin/env python3

"""Check rosbag completeness by exact stamp gaps or fast SQLite aggregates."""

import argparse
import glob
import json
import sqlite3
import struct
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from insight_capture.legacy.composite_bag import iter_messages, read_metadata, session_parts
from insight_capture.quality.topic_rates import nominal_for

DEFAULT_MAX_LOSS_PCT = 0.5
# New bags are startup-trimmed; --warmup remains for legacy recordings.
DEFAULT_WARMUP_S = 0.0


def is_latched_static_topic(topic: str) -> bool:
    """True for static TF: a latched one-shot stream, not a periodic one."""
    return topic.rstrip("/").endswith("/tf_static")


def latched_static_result(name: str, msgs: int) -> Dict[str, object]:
    """Presence check for tf_static; an FPS expectation would be invalid."""
    ok = msgs >= 1
    return {
        "name": name,
        "ok": ok,
        "msgs": msgs,
        "avg_hz": 0.0,
        "audit": "latched_static_presence",
        "expected_messages_min": 1,
        "error": (
            "static transform recorded"
            if ok else "missing static transform; latched topic requires at least 1 message"
        ),
    }


def header_stamp_ns(blob: bytes) -> int:
    sec, nsec = struct.unpack_from("<iI", blob, 4)
    return sec * 1_000_000_000 + nsec


def gap_stats(ts, nominal_hz):
    """Report long gaps and estimate net missing messages across the span."""
    period_ns = 1e9 / nominal_hz
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    events = [(g, i) for i, g in enumerate(gaps) if g > 1.5 * period_ns]
    # VIO publishers are deliberately non-uniform: an 18-22 ms interval is
    # commonly followed by a shorter interval. Summing every long interval
    # therefore invents drops even when the source delivered the full count.
    # Only a complete missing period is evidence of a lost sample. Rounding
    # makes a stable 12.99 Hz source fail against its measured 13 Hz nominal
    # whenever the finite capture window lands just beyond half a period.
    expected = max(1, int((ts[-1] - ts[0]) / period_ns) + 1) if ts else 0
    missing = max(0, expected - len(ts))
    worst = sorted(events, reverse=True)[:3]
    return missing, len(events), [
        f"{g / 1e6:.0f}ms@t+{(ts[i] - ts[0]) / 1e9:.1f}s" for g, i in worst
    ]


def _read_topic_windows(bag_dir: Path) -> Dict[str, Dict[str, int]]:
    """Aggregate topic counts and recorder windows without reading payloads."""
    parts = session_parts(bag_dir)
    if not parts:
        raise ValueError(f"no readable rosbag2 parts in {bag_dir}")
    windows: Dict[str, Dict[str, int]] = {}
    for part in parts:
        db3_files = sorted(glob.glob(str(part / "*.db3")))
        if not db3_files:
            info = read_metadata(part)
            first_ns = int(
                (info.get("starting_time") or {}).get("nanoseconds_since_epoch", 0) or 0
            )
            duration_ns = int((info.get("duration") or {}).get("nanoseconds", 0) or 0)
            last_ns = first_ns + max(duration_ns, 0)
            for entry in info.get("topics_with_message_count") or []:
                name = str((entry.get("topic_metadata") or {}).get("name") or "")
                if not name:
                    continue
                count = int(entry.get("message_count", 0) or 0)
                item = windows.setdefault(
                    name, {"msgs": 0, "first_ns": first_ns, "last_ns": last_ns}
                )
                item["msgs"] += count
                if count:
                    item["first_ns"] = min(item["first_ns"], first_ns)
                    item["last_ns"] = max(item["last_ns"], last_ns)
            continue
        for db3 in db3_files:
            try:
                conn = sqlite3.connect(f"file:{db3}?mode=ro", uri=True)
                with conn:
                    rows = conn.execute(
                        "SELECT topics.name, COUNT(messages.id), MIN(messages.timestamp), MAX(messages.timestamp) "
                        "FROM topics LEFT JOIN messages ON messages.topic_id = topics.id "
                        "GROUP BY topics.id"
                    ).fetchall()
                conn.close()
            except sqlite3.Error as exc:
                raise ValueError(f"cannot read {db3}: {exc}") from exc
            for name, count, first_ns, last_ns in rows:
                if first_ns is None or last_ns is None:
                    windows.setdefault(name, {"msgs": 0, "first_ns": 0, "last_ns": 0})
                    continue
                item = windows.get(name)
                if item is None or item["msgs"] == 0:
                    item = {"msgs": 0, "first_ns": first_ns, "last_ns": last_ns}
                    windows[name] = item
                item["msgs"] += int(count)
                item["first_ns"] = min(item["first_ns"], int(first_ns))
                item["last_ns"] = max(item["last_ns"], int(last_ns))
    return windows


def _selected_topics(bag_dir: Path) -> List[str]:
    manifest_path = bag_dir / "recording_manifest.json"
    try:
        payload = json.loads(manifest_path.read_text())
    except (OSError, ValueError, TypeError):
        return []
    topics = payload.get("selected_topics")
    if not isinstance(topics, list):
        return []
    return [str(topic) for topic in topics if str(topic).startswith("/")]


def _add_missing_selected_topics(
    bag_dir: Path, topics: List[Dict[str, object]]
) -> None:
    present = {str(topic["name"]) for topic in topics}
    for name in _selected_topics(bag_dir):
        if name in present:
            continue
        nominal = nominal_for(name)
        topics.append({
            "name": name,
            "ok": nominal is None,
            "msgs": 0,
            "audit": "selected_topic_presence" if nominal is not None else "source_silent",
            "error": (
                "selected continuous topic is absent from the bag"
                if nominal is not None
                else "selected conditional/event topic was silent; loss cannot be calculated"
            ),
        })
    topics.sort(key=lambda topic: str(topic["name"]))


def _analyze_fast(
    bag_dir: Path, max_loss_pct: float, warmup_s: float,
) -> List[Dict[str, object]]:
    """Fast per-topic loss estimate using count and each topic's own span."""
    windows = _read_topic_windows(bag_dir)
    topics: List[Dict[str, object]] = []
    for name in sorted(windows):
        item = windows[name]
        msgs = item["msgs"]
        span_s = max(0.0, (item["last_ns"] - item["first_ns"]) / 1e9)
        avg_hz = (msgs - 1) / span_s if span_s > 0 and msgs > 1 else 0.0
        if is_latched_static_topic(name):
            topics.append(latched_static_result(name, msgs))
            continue
        nominal = nominal_for(name)
        if msgs == 0:
            topics.append({
                "name": name, "ok": nominal is None, "msgs": 0,
                "audit": "topic_presence" if nominal is not None else "source_silent",
                "error": (
                    "continuous topic exists in bag metadata but contains no messages"
                    if nominal is not None
                    else "conditional/event topic was silent; loss cannot be calculated"
                ),
            })
            continue
        if nominal is None:
            # Event streams remain visible but cannot be checked for drops.
            topics.append({
                "name": name,
                "ok": True,
                "msgs": msgs,
                "avg_hz": round(avg_hz, 2),
                "audit": "unconfigured_rate",
                "error": "recorded; no nominal rate configured, so drops cannot be calculated",
            })
            continue
        if msgs < 2:
            topics.append({
                "name": name, "ok": False, "msgs": msgs,
                "error": f"only {msgs} message(s)",
            })
            continue
        # Include both endpoints and use this topic's span, not the bag's.
        expected = max(1, int(span_s * nominal - warmup_s * nominal) + 1)
        missing = max(0, expected - msgs)
        loss_pct = missing / expected * 100 if expected > 0 else 0.0
        topics.append({
            "name": name,
            "ok": loss_pct <= max_loss_pct,
            "audit": "timestamp_aggregate",
            "msgs": msgs,
            "avg_hz": round(avg_hz, 2),
            "nominal_hz": nominal,
            "missing": missing,
            "loss_pct": round(loss_pct, 2),
        })
    _add_missing_selected_topics(bag_dir, topics)
    return topics


def _analyze_deep(
    bag_dir: Path, max_loss_pct: float, warmup_s: float,
) -> List[Dict[str, object]]:
    """Original per-message stamp-gap scan (slow: reads the whole table)."""
    parts = session_parts(bag_dir)
    if len(parts) != 1 or parts[0] != bag_dir or not list(parts[0].glob("*.db3")):
        return _analyze_deep_storage(bag_dir, max_loss_pct, warmup_s)
    db3 = sorted(glob.glob(str(bag_dir / "*.db3")))
    if not db3:
        raise ValueError(f"no .db3 file in {bag_dir}")
    try:
        conn = sqlite3.connect(f"file:{db3[0]}?mode=ro", uri=True)
        topic_names = dict(conn.execute("SELECT id, name FROM topics"))
    except sqlite3.Error as exc:
        raise ValueError(f"cannot read {db3[0]}: {exc}") from exc

    topics: List[Dict[str, object]] = []
    with conn:
        for tid, name in sorted(topic_names.items(), key=lambda kv: kv[1]):
            if is_latched_static_topic(name):
                msgs = int(conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE topic_id=?", (tid,)
                ).fetchone()[0])
                topics.append(latched_static_result(name, msgs))
                continue
            nominal = nominal_for(name)
            if nominal is None:
                msgs = int(conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE topic_id=?", (tid,)
                ).fetchone()[0])
                topics.append({
                    "name": name,
                    "ok": True,
                    "msgs": msgs,
                    "avg_hz": 0.0,
                    "audit": "unconfigured_rate_presence" if msgs > 0 else "source_silent",
                    "error": (
                        "recorded; event/unknown-rate topic checked for presence only"
                        if msgs > 0 else "conditional/event topic was silent; loss cannot be calculated"
                    ),
                })
                continue
            # Read only the 12-byte stamp instead of full image payloads.
            rows = conn.execute(
                "SELECT timestamp, substr(data,1,12) FROM messages "
                "WHERE topic_id=? ORDER BY timestamp",
                (tid,),
            ).fetchall()
            if len(rows) < 2:
                topics.append({
                    "name": name, "ok": False, "msgs": len(rows),
                    "error": f"only {len(rows)} message(s)",
                })
                continue
            stamps = [header_stamp_ns(r[1]) for r in rows]
            cutoff = stamps[0] + int(warmup_s * 1e9)
            settled = [t for t in stamps if t >= cutoff] or stamps
            source_span = (settled[-1] - settled[0]) / 1e9
            avg_hz = (len(settled) - 1) / source_span if source_span > 0 else 0.0
            missing, events, worst = gap_stats(settled, nominal)
            loss_pct = missing / (len(settled) + missing) * 100 if missing else 0.0
            topics.append({
                "name": name,
                "ok": loss_pct <= max_loss_pct,
                "audit": "header_stamp_gaps",
                "msgs": len(rows),
                "avg_hz": round(avg_hz, 2),
                "nominal_hz": nominal,
                "missing": missing,
                "loss_pct": round(loss_pct, 2),
                "gap_events": events,
                "worst_gaps": worst,
            })
    _add_missing_selected_topics(bag_dir, topics)
    return topics


def _analyze_deep_storage(
    bag_dir: Path, max_loss_pct: float, warmup_s: float,
) -> List[Dict[str, object]]:
    """Scan composite/MCAP parts through the rosbag2 storage API."""
    windows = _read_topic_windows(bag_dir)
    names = sorted(windows)
    counts = {name: 0 for name in names}
    stamps = {name: [] for name in names if nominal_for(name) is not None}
    for name, raw, record_stamp in iter_messages(bag_dir, names):
        counts[name] = counts.get(name, 0) + 1
        if name in stamps:
            try:
                stamp = header_stamp_ns(raw)
            except (struct.error, TypeError):
                stamp = int(record_stamp)
            stamps[name].append(stamp)

    topics: List[Dict[str, object]] = []
    for name in names:
        msgs = counts.get(name, 0)
        if is_latched_static_topic(name):
            topics.append(latched_static_result(name, msgs))
            continue
        nominal = nominal_for(name)
        if nominal is None:
            topics.append({
                "name": name,
                "ok": True,
                "msgs": msgs,
                "avg_hz": 0.0,
                "audit": "unconfigured_rate_presence" if msgs > 0 else "source_silent",
                "error": (
                    "recorded; event/unknown-rate topic checked for presence only"
                    if msgs > 0 else "conditional/event topic was silent; loss cannot be calculated"
                ),
            })
            continue
        values = stamps.get(name, [])
        if len(values) < 2:
            topics.append({
                "name": name, "ok": False, "msgs": len(values),
                "error": f"only {len(values)} message(s)",
            })
            continue
        values.sort()
        cutoff = values[0] + int(warmup_s * 1e9)
        settled = [stamp for stamp in values if stamp >= cutoff] or values
        source_span = (settled[-1] - settled[0]) / 1e9
        avg_hz = (len(settled) - 1) / source_span if source_span > 0 else 0.0
        missing, events, worst = gap_stats(settled, nominal)
        loss_pct = missing / (len(settled) + missing) * 100 if missing else 0.0
        topics.append({
            "name": name,
            "ok": loss_pct <= max_loss_pct,
            "audit": "header_stamp_gaps",
            "msgs": msgs,
            "avg_hz": round(avg_hz, 2),
            "nominal_hz": nominal,
            "missing": missing,
            "loss_pct": round(loss_pct, 2),
            "gap_events": events,
            "worst_gaps": worst,
        })
    _add_missing_selected_topics(bag_dir, topics)
    return topics


def analyze_bag(
    bag_dir: Path,
    max_loss_pct: float = DEFAULT_MAX_LOSS_PCT,
    warmup_s: float = DEFAULT_WARMUP_S,
    deep: bool = True,
) -> Dict[str, object]:
    """Return a JSON-ready exact or fast integrity report for one bag."""
    bag_dir = Path(bag_dir)
    method = "deep_scan"
    if deep:
        topics = _analyze_deep(bag_dir, max_loss_pct, warmup_s)
    else:
        topics = _analyze_fast(bag_dir, max_loss_pct, warmup_s)
        method = "timestamp_aggregates"

    failed = [t["name"] for t in topics if not t["ok"]]
    network_audit = None
    try:
        network_audit = json.loads((bag_dir / "recording_network_audit.json").read_text())
    except (OSError, ValueError, TypeError):
        pass
    return {
        "bag": bag_dir.name,
        "path": str(bag_dir),
        "ok": bool(topics) and not failed,
        "failed_topics": failed,
        "topics": topics,
        "network_audit": network_audit,
        "method": method,
        "max_loss_pct": max_loss_pct,
        "warmup_s": warmup_s,
        "checked_at_epoch_s": time.time(),
    }


def find_bag(bag_arg: Optional[str]) -> Path:
    root = Path(__file__).resolve().parents[3]
    if bag_arg:
        return Path(bag_arg)
    bags = sorted(
        (p for p in (root / "rosbags").iterdir()
         if p.is_dir() and not p.name.startswith("_")),
        key=lambda p: p.name,
    )
    if not bags:
        sys.exit("no bags found under rosbags/")
    return bags[-1]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bag", nargs="?", help="bag directory (default: newest under rosbags/)")
    parser.add_argument("--max-loss", type=float, default=DEFAULT_MAX_LOSS_PCT,
                        help=f"max tolerated loss %% per topic (default {DEFAULT_MAX_LOSS_PCT})")
    parser.add_argument("--warmup", type=float, default=DEFAULT_WARMUP_S,
                        help=f"seconds forgiven at each topic's start (default {DEFAULT_WARMUP_S})")
    parser.add_argument("--fast", action="store_true",
                        help="fast per-topic timestamp aggregate; does not read message payloads")
    parser.add_argument("--deep", action="store_true",
                        help="deprecated compatibility alias; exact mode is already the default")
    args = parser.parse_args()

    bag_dir = find_bag(args.bag)
    print(f"bag: {bag_dir}\n")
    try:
        report = analyze_bag(bag_dir, max_loss_pct=args.max_loss,
                             warmup_s=args.warmup, deep=args.deep or not args.fast)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)

    for topic in report["topics"]:
        verdict = "ok  " if topic["ok"] else "FAIL"
        print(f"{verdict}  {topic['name']}")
        if "error" in topic:
            print(f"      {topic['error']}")
            continue
        line = (f"      msgs={topic['msgs']} avg={topic['avg_hz']}Hz (nominal {topic['nominal_hz']:g})"
                f" missing={topic['missing']} ({topic['loss_pct']}%)")
        if "gap_events" in topic:
            line += f" gap_events={topic['gap_events']}"
        if topic.get("worst_gaps"):
            line += f" worst={topic['worst_gaps']}"
        print(line)

    network_audit = report.get("network_audit")
    if isinstance(network_audit, dict) and not network_audit.get("ok", True):
        issues = network_audit.get("issues") or {}
        print(f"WARN  kernel receive audit reported counter increments: {issues}")

    print()
    if not report["ok"]:
        print(f"RESULT: FAIL -- {len(report['failed_topics'])} topic(s) above {args.max_loss}% loss.")
        print("Triage: docs/USAGE.md §6.3 (dropped frames during recording)")
        sys.exit(1)
    print(f"RESULT: OK -- all checked topics within {args.max_loss}% loss.")


if __name__ == "__main__":
    main()
