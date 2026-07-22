#!/usr/bin/env python3

"""Frame-drop / completeness check for a rosbag2 (sqlite3) recording.

Two modes:

  - default (exact): per-message scan. Reads message receive times AND
    sensor header stamps (parsed from the first 12 bytes of the CDR blob:
    4-byte encapsulation header, then int32 sec + uint32 nsec) so the two
    failure modes can be told apart:

      - gaps in *header stamps*  -> those frames never reached this process
        (camera-side stall, or DDS/UDP loss on the way here)
      - gaps only in *recv times* while stamps are complete -> delivery was
        bursty but nothing was lost (normal for batched IMU delivery)

    The 2026-07-07 investigation used exactly this distinction to pin
    10-24% image loss on the kernel's 208KB default UDP receive buffer vs
    510KB best-effort image samples (fix: /etc/sysctl.d/
    99-dds-rx-buffers.conf, written by scripts/setup_host.sh). camera_info
    (tiny, RELIABLE) arriving complete while images dropped ruled the
    cameras themselves out. Keep --deep for that kind of triage; it also
    reports per-gap detail (gap_events / worst_gaps).

  - --fast: per-topic message counts and whole-bag duration from
    metadata.yaml only. This is useful for a quick estimate, but it is not
    an integrity verdict: independently stopped recorders can leave a
    trailing IMU/VIO-only interval, which makes whole-bag duration falsely
    report missing image frames even when every image header is continuous.

    Bags without a readable metadata.yaml (e.g. power-loss orphans before
    recovery) automatically use exact mode even when --fast was requested.

Used two ways:
  - CLI (host or `docker exec`):
      python3 scripts/check_bag.py                      # newest bag in rosbags/
      python3 scripts/check_bag.py rosbags/<bag_dir>    # specific bag
      python3 scripts/check_bag.py --fast ...           # metadata estimate only
      python3 scripts/check_bag.py --max-loss 1.0 ...   # exit 1 above this %
  - imported by the dashboard backend (analyze_bag) for the Scoring page's
    "Verify Integrity" button and the Bags page integrity badge. These use
    the exact default mode.

Exit code: 0 = every checked topic within --max-loss, 1 = drops found,
2 = bag unreadable. Suitable for scripting after important recordings.

No ROS dependencies -- plain sqlite3/struct/yaml, runs on host or container.
"""

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
    # Labeled 100Hz but the VIO estimator's own compute cycle never quite
    # hits that: a bare `ros2 topic hz` with zero recorder/dashboard code
    # attached measures a steady 99.19-99.31Hz on all 3 cameras (verified
    # 2026-07-14, std dev ~0.0025s -- not sporadic loss, a real cycle-time
    # floor). 99.0 leaves headroom under the observed floor so hitting that
    # ceiling isn't reported as loss, while still catching a genuine
    # additional drop below it.
    ("vio_100hz", 99.0),
    ("vio_image_cov", 20.0),
    ("tf_static", None),
]

DEFAULT_MAX_LOSS_PCT = 0.5
# Subscriptions settling right after recording start used to produce
# harmless gaps that this allowance forgave -- but it did so by subtracting
# a flat allowance from the expected count regardless of WHERE the shortfall
# actually fell, so it just as easily masked real mid-recording loss as it
# forgave startup jitter (e.g. IMU averaging 395-398Hz still read 0% loss).
# RecordingManager._trim_startup_skew (post_processing.py) now cuts that
# startup window out of the merged bag at merge time instead, up to a 2s
# cap, so every topic reaching check_bag has either fully started or is
# fully absent -- no more startup-jitter class of gap to forgive here.
# Default 0 so loss is reported honestly; pass --warmup for bags recorded
# before 2026-07-14 (no trim applied) if they show stale startup-only gaps.
DEFAULT_WARMUP_S = 0.0


def nominal_for(topic: str) -> Optional[float]:
    for fragment, hz in NOMINAL_HZ:
        if fragment in topic:
            if fragment == "camera_info":
                return 30.0 if "color" in topic else 20.0
            return hz
    return None


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


def _read_metadata_counts(bag_dir: Path):
    """(duration_s, {topic: count}) from metadata.yaml, or None if unusable."""
    meta_file = bag_dir / "metadata.yaml"
    if not meta_file.is_file():
        return None
    try:
        import yaml
        info = yaml.safe_load(meta_file.read_text())["rosbag2_bagfile_information"]
        duration_s = info["duration"]["nanoseconds"] / 1e9
        counts = {
            entry["topic_metadata"]["name"]: int(entry["message_count"])
            for entry in info["topics_with_message_count"]
        }
    except Exception:
        return None
    if duration_s <= 0 or not counts:
        return None
    return duration_s, counts


def _analyze_fast(
    bag_dir: Path, duration_s: float, counts: Dict[str, int],
    max_loss_pct: float, warmup_s: float,
) -> List[Dict[str, object]]:
    """Rate + loss %% per topic from counts alone (no database read)."""
    topics: List[Dict[str, object]] = []
    for name in sorted(counts):
        nominal = nominal_for(name)
        if nominal is None:
            continue
        msgs = counts[name]
        if msgs < 2:
            topics.append({
                "name": name, "ok": False, "msgs": msgs,
                "error": f"only {msgs} message(s)",
            })
            continue
        avg_hz = msgs / duration_s
        # Expected over the whole duration, minus a warmup allowance (frames
        # a settling subscription may legitimately have missed at the start).
        expected = duration_s * nominal
        missing = max(0, round(expected - warmup_s * nominal) - msgs)
        loss_pct = missing / expected * 100 if expected > 0 else 0.0
        topics.append({
            "name": name,
            "ok": loss_pct <= max_loss_pct,
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
            nominal = nominal_for(name)
            if nominal is None:
                continue
            # substr(): only the 12 stamp bytes leave sqlite instead of
            # full image blobs -- measured 11.6s -> 8.2s cold on a 3GB bag.
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
    """Analyze one bag directory; returns a JSON-serializable report.

    Raises ValueError when the bag has no readable .db3 file (deep mode /
    fallback) or no usable metadata.

    Exact mode (default) scans header stamps. Fast mode is an explicitly
    requested metadata estimate; bags without readable metadata fall back
    to the exact scan.
    """
    bag_dir = Path(bag_dir)
    method = "deep_scan"
    if deep:
        topics = _analyze_deep(bag_dir, max_loss_pct, warmup_s)
    else:
        meta = _read_metadata_counts(bag_dir)
        if meta is not None:
            duration_s, counts = meta
            topics = _analyze_fast(bag_dir, duration_s, counts, max_loss_pct, warmup_s)
            method = "metadata_counts"
        else:
            topics = _analyze_deep(bag_dir, max_loss_pct, warmup_s)

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
                        help="metadata-only rate estimate; not an exact integrity verdict")
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

    if report["method"] == "deep_scan" and args.fast:
        print("(no usable metadata.yaml -- fell back to the exact scan)\n")
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
