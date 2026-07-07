#!/usr/bin/env python3

"""Frame-drop / completeness check for a rosbag2 (sqlite3) recording.

Reads message receive times AND sensor header stamps (parsed straight from
the CDR blob: 4-byte encapsulation header, then int32 sec + uint32 nsec) so
the two failure modes can be told apart:

  - gaps in *header stamps*  -> those frames never reached this process
    (camera-side stall, or DDS/UDP loss on the way here)
  - gaps only in *recv times* while stamps are complete -> delivery was
    bursty but nothing was lost (normal for batched IMU delivery)

The 2026-07-07 investigation used exactly this distinction to pin 10-24%
image loss on the kernel's 208KB default UDP receive buffer vs 510KB
best-effort image samples (fix: /etc/sysctl.d/99-dds-rx-buffers.conf,
written by scripts/setup_host.sh). camera_info (tiny, RELIABLE) arriving
complete while images dropped ruled the cameras themselves out.

Usage:
  python3 scripts/check_bag.py                      # newest bag in rosbags/
  python3 scripts/check_bag.py rosbags/<bag_dir>    # specific bag
  python3 scripts/check_bag.py --max-loss 1.0 ...   # exit 1 above this %

Exit code: 0 = every checked topic within --max-loss, 1 = drops found,
2 = bag unreadable. Suitable for scripting after important recordings.

No ROS dependencies -- plain sqlite3 + struct, runs on host or container.
"""

import argparse
import glob
import sqlite3
import struct
import sys
from pathlib import Path

# Expected publish rate by topic-name fragment; extend when new sensors
# join the fleet. None = skip rate analysis (event-style topics).
NOMINAL_HZ = [
    ("/imu", 400.0),
    ("image_rect_raw/compressed", 30.0),
    ("image_rect_raw", 20.0),
    ("camera_info", None),  # follows its image stream; resolved below
    ("vio_100hz", 100.0),
    ("vio_image_cov", 20.0),
    ("tf_static", None),
]


def nominal_for(topic: str) -> float:
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


def find_db(bag_arg):
    root = Path(__file__).resolve().parents[1]
    if bag_arg:
        bag_dir = Path(bag_arg)
    else:
        bags = sorted(
            (p for p in (root / "rosbags").iterdir()
             if p.is_dir() and not p.name.startswith("_")),
            key=lambda p: p.name,
        )
        if not bags:
            sys.exit("no bags found under rosbags/")
        bag_dir = bags[-1]
    db3 = sorted(glob.glob(str(bag_dir / "*.db3")))
    if not db3:
        print(f"ERROR: no .db3 file in {bag_dir}", file=sys.stderr)
        sys.exit(2)
    return bag_dir, db3[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bag", nargs="?", help="bag directory (default: newest under rosbags/)")
    parser.add_argument("--max-loss", type=float, default=0.5,
                        help="max tolerated header-stamp loss %% per topic (default 0.5)")
    parser.add_argument("--warmup", type=float, default=2.0,
                        help="seconds to ignore at each topic's start (default 2.0) -- "
                             "subscriptions settling right after recording start produce "
                             "harmless gaps that would otherwise mask real mid-recording loss")
    args = parser.parse_args()

    bag_dir, db_path = find_db(args.bag)
    print(f"bag: {bag_dir}\n")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        topics = dict(conn.execute("SELECT id, name FROM topics"))
    except sqlite3.Error as exc:
        print(f"ERROR: cannot read {db_path}: {exc}", file=sys.stderr)
        sys.exit(2)

    failed = []
    for tid, name in sorted(topics.items(), key=lambda kv: kv[1]):
        nominal = nominal_for(name)
        if nominal is None:
            continue
        rows = conn.execute(
            "SELECT timestamp, data FROM messages WHERE topic_id=? ORDER BY timestamp",
            (tid,),
        ).fetchall()
        if len(rows) < 2:
            print(f"FAIL  {name}: only {len(rows)} message(s)")
            failed.append(name)
            continue
        recv = [r[0] for r in rows]
        stamps = [header_stamp_ns(r[1]) for r in rows]
        span = (recv[-1] - recv[0]) / 1e9
        avg_hz = (len(recv) - 1) / span if span > 0 else 0.0
        cutoff = stamps[0] + int(args.warmup * 1e9)
        settled = [t for t in stamps if t >= cutoff] or stamps
        missing, events, worst = gap_stats(settled, nominal)
        loss_pct = missing / (len(rows) + missing) * 100 if missing else 0.0
        ok = loss_pct <= args.max_loss
        verdict = "ok  " if ok else "FAIL"
        print(f"{verdict}  {name}")
        print(f"      msgs={len(rows)} avg={avg_hz:.2f}Hz (nominal {nominal:g})"
              f" missing={missing} ({loss_pct:.1f}%) gap_events={events}"
              + (f" worst={worst}" if worst else ""))
        if not ok:
            failed.append(name)

    print()
    if failed:
        print(f"RESULT: FAIL -- {len(failed)} topic(s) above {args.max_loss}% loss.")
        print("Triage: docs/USAGE.md §常见问题 · 录制掉帧")
        sys.exit(1)
    print(f"RESULT: OK -- all checked topics within {args.max_loss}% loss.")


if __name__ == "__main__":
    main()
