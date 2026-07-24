#!/usr/bin/env python3

"""Check rosbag completeness by exact stamp gaps or fast SQLite aggregates."""

import argparse
import glob
import sqlite3
import struct
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

# Expected publish rate by topic-name fragment; extend when new sensors
# join the fleet. None = skip rate analysis (event-style topics).
NOMINAL_HZ = [
    ("/imu", 400.0),
    ("image_rect_raw/compressed", 30.0),
    ("image_rect_raw", 20.0),
    ("camera_info", None),  # follows its image stream; resolved below
    # VIO's measured steady-state ceiling is slightly below its 100 Hz label.
    ("vio_100hz", 99.0),
    ("vio_image_cov", 20.0),
]

DEFAULT_MAX_LOSS_PCT = 0.5
# New bags are startup-trimmed; --warmup remains for legacy recordings.
DEFAULT_WARMUP_S = 0.0


def nominal_for(topic: str) -> Optional[float]:
    for fragment, hz in NOMINAL_HZ:
        if fragment in topic:
            if fragment == "camera_info":
                return 30.0 if "color" in topic else 20.0
            return hz
    return None


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
            "static transform recorded; latched topic requires at least 1 message (no FPS applies)"
            if ok else "missing static transform; latched topic requires at least 1 message"
        ),
    }


def header_stamp_ns(blob: bytes) -> int:
    sec, nsec = struct.unpack_from("<iI", blob, 4)
    return sec * 1_000_000_000 + nsec


def gap_stats(ts, nominal_hz):
    """Estimate missing messages from inter-arrival gaps > 1.5 periods."""
    period_ns = 1e9 / nominal_hz
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    events = [(g, i) for i, g in enumerate(gaps) if g > 1.5 * period_ns]
    missing = sum(round(g / period_ns) - 1 for g, _ in events)
    worst = sorted(events, reverse=True)[:3]
    return missing, len(events), [
        f"{g / 1e6:.0f}ms@t+{(ts[i] - ts[0]) / 1e9:.1f}s" for g, i in worst
    ]


def _read_topic_windows(bag_dir: Path) -> Dict[str, Dict[str, int]]:
    """Aggregate topic counts and recorder windows without reading payloads."""
    db3_files = sorted(glob.glob(str(bag_dir / "*.db3")))
    if not db3_files:
        raise ValueError(f"no .db3 file in {bag_dir}")
    windows: Dict[str, Dict[str, int]] = {}
    for db3 in db3_files:
        try:
            conn = sqlite3.connect(f"file:{db3}?mode=ro", uri=True)
            with conn:
                rows = conn.execute(
                    "SELECT topics.name, COUNT(*), MIN(messages.timestamp), MAX(messages.timestamp) "
                    "FROM messages JOIN topics ON messages.topic_id = topics.id "
                    "GROUP BY messages.topic_id"
                ).fetchall()
            conn.close()
        except sqlite3.Error as exc:
            raise ValueError(f"cannot read {db3}: {exc}") from exc
        for name, count, first_ns, last_ns in rows:
            item = windows.setdefault(name, {"msgs": 0, "first_ns": first_ns, "last_ns": last_ns})
            item["msgs"] += int(count)
            item["first_ns"] = min(item["first_ns"], int(first_ns))
            item["last_ns"] = max(item["last_ns"], int(last_ns))
    return windows


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
        expected = max(1, round(span_s * nominal + 1 - warmup_s * nominal))
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
    return topics


def _analyze_deep(
    bag_dir: Path, max_loss_pct: float, warmup_s: float,
) -> List[Dict[str, object]]:
    """Original per-message stamp-gap scan (slow: reads the whole table)."""
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
            recv = [r[0] for r in rows]
            stamps = [header_stamp_ns(r[1]) for r in rows]
            span = (recv[-1] - recv[0]) / 1e9
            avg_hz = (len(recv) - 1) / span if span > 0 else 0.0
            cutoff = stamps[0] + int(warmup_s * 1e9)
            settled = [t for t in stamps if t >= cutoff] or stamps
            missing, events, worst = gap_stats(settled, nominal)
            loss_pct = missing / (len(rows) + missing) * 100 if missing else 0.0
            topics.append({
                "name": name,
                "ok": loss_pct <= max_loss_pct,
                "msgs": len(rows),
                "avg_hz": round(avg_hz, 2),
                "nominal_hz": nominal,
                "missing": missing,
                "loss_pct": round(loss_pct, 2),
                "gap_events": events,
                "worst_gaps": worst,
            })
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
    return {
        "bag": bag_dir.name,
        "path": str(bag_dir),
        "ok": bool(topics) and not failed,
        "failed_topics": failed,
        "topics": topics,
        "method": method,
        "max_loss_pct": max_loss_pct,
        "warmup_s": warmup_s,
        "checked_at_epoch_s": time.time(),
    }


def find_bag(bag_arg: Optional[str]) -> Path:
    root = Path(__file__).resolve().parents[1]
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

    print()
    if not report["ok"]:
        print(f"RESULT: FAIL -- {len(report['failed_topics'])} topic(s) above {args.max_loss}% loss.")
        print("Triage: docs/USAGE.md §6.3 (dropped frames during recording)")
        sys.exit(1)
    print(f"RESULT: OK -- all checked topics within {args.max_loss}% loss.")


if __name__ == "__main__":
    main()
