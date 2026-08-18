#!/usr/bin/python3
"""Score VIO trajectory quality from pose covariance and sustained spikes."""

import argparse
import json
import math
import sys
from pathlib import Path

import rosbag2_py
from rclpy.serialization import deserialize_message
from rosidl_runtime_py.utilities import get_message


DEFAULT_TOPIC = "/insight7_a/camera/vio_image_cov"

# Local calibration; recompute after material hardware or environment changes.
DEFAULT_CAL_MEDIAN_L = -3.61310
DEFAULT_CAL_SIGMA_L = 0.412

SPIKE_FACTOR = 8.0            # spike threshold = SPIKE_FACTOR * median trace
TRANSIENT_MAX_FRAMES = 2      # runs at most this long count as transient
TRANSIENT_PENALTY_EACH = 1.5
TRANSIENT_PENALTY_CAP = 5.0
SUSTAINED_PENALTY_PER_S = 35.0
EPISODE_BAD_RUN_CAP_S = 1.0   # sustained run longer than this caps the score
EPISODE_CAP_SCORE = 40.0

QUALITY_TIERS = [
    (90, "Excellent"),
    (70, "Good"),
    (50, "Fair"),
    (0, "Poor"),
]


def covariance_trace(cov36: list) -> float:
    """Sum of the diagonal of a 6x6 row-major covariance matrix."""
    return sum(cov36[i * 7] for i in range(6))


def percentile(sorted_data: list, p: float) -> float:
    n = len(sorted_data)
    if n == 0:
        return 0.0
    k = (n - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, n - 1)
    return sorted_data[lo] + (sorted_data[hi] - sorted_data[lo]) * (k - lo)


def quality_label(score: float) -> str:
    for threshold, label in QUALITY_TIERS:
        if score >= threshold:
            return label
    return "Poor"


def spike_runs(traces: list, threshold: float) -> list:
    """Lengths (in frames) of consecutive runs above threshold."""
    runs = []
    run = 0
    for value in traces:
        if value > threshold:
            run += 1
        elif run:
            runs.append(run)
            run = 0
    if run:
        runs.append(run)
    return runs


def compute_stats(stamps_s: list, traces: list, cal_median_l: float, cal_sigma_l: float) -> dict:
    n = len(traces)
    mean = sum(traces) / n
    std = math.sqrt(sum((x - mean) ** 2 for x in traces) / n)
    s = sorted(traces)
    p50, p90, p99 = percentile(s, 50), percentile(s, 90), percentile(s, 99)

    if p50 <= 0.0 or p90 <= 0.0:
        raise ValueError(
            "median covariance trace is zero -- covariance not populated on this topic"
        )

    # Median frame interval; robust against stamp gaps and gives
    # single-frame runs a nonzero duration.
    if n >= 2:
        deltas = sorted(b - a for a, b in zip(stamps_s, stamps_s[1:]) if b > a)
        frame_dt = percentile(deltas, 50) if deltas else 0.0
    else:
        frame_dt = 0.0
    duration_s = n * frame_dt

    bulk_l = 0.5 * (math.log10(p50) + math.log10(p90))
    z = (cal_median_l - bulk_l) / cal_sigma_l
    base = 100.0 / (1.0 + math.exp(-(1.1 + 1.2 * z)))

    threshold = SPIKE_FACTOR * p50
    runs = spike_runs(traces, threshold)
    transient_runs = [r for r in runs if r <= TRANSIENT_MAX_FRAMES]
    sustained_runs = [r for r in runs if r > TRANSIENT_MAX_FRAMES]

    transient_penalty = min(
        TRANSIENT_PENALTY_CAP,
        TRANSIENT_PENALTY_EACH * len(transient_runs),
    )
    sustained_frames = sum(sustained_runs)
    sustained_seconds = sustained_frames * frame_dt
    sustained_penalty = SUSTAINED_PENALTY_PER_S * sustained_seconds
    longest_bad_run_s = max(sustained_runs, default=0) * frame_dt

    score = max(0.0, min(100.0, base - transient_penalty - sustained_penalty))
    episode_capped = longest_bad_run_s > EPISODE_BAD_RUN_CAP_S
    if episode_capped:
        score = min(score, EPISODE_CAP_SCORE)

    bad_frames = sum(runs)
    return {
        "n_poses": n,
        "duration_s": round(duration_s, 3),
        "mean_trace": mean,
        "std_trace": std,
        "min_trace": s[0],
        "max_trace": s[-1],
        "p50_trace": p50,
        "p90_trace": p90,
        "p99_trace": p99,
        "base_score": round(base, 2),
        "transient_run_count": len(transient_runs),
        "transient_penalty": round(transient_penalty, 2),
        "bad_run_count": len(sustained_runs),
        "bad_run_seconds": round(sustained_seconds, 3),
        "longest_bad_run_s": round(longest_bad_run_s, 3),
        "sustained_penalty": round(sustained_penalty, 2),
        "usable_fraction": round(1.0 - bad_frames / n, 4),
        "episode_capped": episode_capped,
        "cal_median_log10_trace": cal_median_l,
        "cal_sigma_log10_trace": cal_sigma_l,
        "score": round(score, 2),
        "quality": quality_label(score),
    }


def open_reader(bag_path: str, topic: str) -> rosbag2_py.SequentialReader:
    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=bag_path, storage_id=""),
        rosbag2_py.ConverterOptions(
            input_serialization_format="cdr",
            output_serialization_format="cdr",
        ),
    )
    reader.set_filter(rosbag2_py.StorageFilter(topics=[topic]))
    return reader


def get_topic_type(reader: rosbag2_py.SequentialReader, topic: str) -> str | None:
    for t in reader.get_all_topics_and_types():
        if t.name == topic:
            return t.type
    return None


def collect_traces(
    reader: rosbag2_py.SequentialReader,
    topic: str,
    msg_type,
    verbose: bool,
) -> tuple:
    stamps_s = []
    traces = []
    while reader.has_next():
        t_name, raw, stamp_ns = reader.read_next()
        if t_name != topic:
            continue
        msg = deserialize_message(raw, msg_type)
        trace = covariance_trace(list(msg.pose.covariance))
        stamps_s.append(stamp_ns / 1e9)
        traces.append(trace)
        if verbose:
            stamp = msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9
            print(f"  [{len(traces):6d}]  t={stamp:.3f}  trace={trace:.6e}")
    return stamps_s, traces


def print_report(stats: dict, bag_path: str, topic: str) -> None:
    width = 58
    print("=" * width)
    print("  Trajectory Quality Report")
    print(f"  Bag   : {bag_path}")
    print(f"  Topic : {topic}")
    print("-" * width)
    print(f"  Poses / duration : {stats['n_poses']}  /  {stats['duration_s']:.1f}s")
    print(f"  p50  cov trace   : {stats['p50_trace']:.6e}")
    print(f"  p90  cov trace   : {stats['p90_trace']:.6e}")
    print(f"  p99  cov trace   : {stats['p99_trace']:.6e}")
    print(f"  Max  cov trace   : {stats['max_trace']:.6e}")
    print("-" * width)
    print(f"  Base score       : {stats['base_score']:.1f}  "
          f"(cal: median_L={stats['cal_median_log10_trace']}, "
          f"sigma={stats['cal_sigma_log10_trace']})")
    print(f"  Transient blips  : {stats['transient_run_count']} run(s)  "
          f"-> -{stats['transient_penalty']:.1f}")
    print(f"  Sustained bad    : {stats['bad_run_count']} run(s), "
          f"{stats['bad_run_seconds']:.2f}s total, "
          f"longest {stats['longest_bad_run_s']:.2f}s  "
          f"-> -{stats['sustained_penalty']:.1f}")
    if stats["episode_capped"]:
        print(f"  EPISODE CAP      : bad run > {EPISODE_BAD_RUN_CAP_S:.0f}s "
              f"-> score capped at {EPISODE_CAP_SCORE:.0f}")
    print(f"  Usable fraction  : {100.0 * stats['usable_fraction']:.1f}%")
    print("-" * width)
    print(f"  Score            : {stats['score']:.1f} / 100  [{stats['quality']}]")
    print("=" * width)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="traj_score",
        description="Evaluate VIO/SLAM trajectory quality from a ROS2 bag.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("bag_path", help="Path to the ROS2 bag directory or .db3 file.")
    parser.add_argument(
        "--topic",
        "-t",
        default=DEFAULT_TOPIC,
        help=f"Topic name (default: {DEFAULT_TOPIC})",
    )
    parser.add_argument(
        "--cal-median-log10-trace",
        type=float,
        default=DEFAULT_CAL_MEDIAN_L,
        help=(
            "Calibration centre for the base-score logistic in log10 covariance-trace units "
            f"(higher is more lenient). Default: {DEFAULT_CAL_MEDIAN_L}"
        ),
    )
    parser.add_argument(
        "--cal-sigma-log10-trace",
        type=float,
        default=DEFAULT_CAL_SIGMA_L,
        help=(
            "Calibration: robust sigma of log10 trace in decades. "
            f"Default: {DEFAULT_CAL_SIGMA_L}"
        ),
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Print per-pose trace values.")
    parser.add_argument("--list-topics", "-l", action="store_true", help="List all topics in the bag and exit.")
    parser.add_argument("--json", "-j", metavar="FILE", help="Save full results to a JSON file.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    try:
        reader = open_reader(args.bag_path, args.topic)
    except Exception as exc:
        print(f"Error opening bag '{args.bag_path}': {exc}", file=sys.stderr)
        sys.exit(1)

    if args.list_topics:
        print("Topics in bag:")
        for topic in reader.get_all_topics_and_types():
            print(f"  {topic.name}  [{topic.type}]")
        return

    topic_type_str = get_topic_type(reader, args.topic)
    if topic_type_str is None:
        print(f"Topic '{args.topic}' not found in bag.", file=sys.stderr)
        print("Available topics:", file=sys.stderr)
        for topic in reader.get_all_topics_and_types():
            print(f"  {topic.name}  [{topic.type}]", file=sys.stderr)
        sys.exit(1)

    msg_type = get_message(topic_type_str)
    print(f"Reading '{args.topic}'  [{topic_type_str}]")

    stamps_s, traces = collect_traces(reader, args.topic, msg_type, args.verbose)
    if not traces:
        print("No messages found on this topic.", file=sys.stderr)
        sys.exit(1)

    try:
        stats = compute_stats(
            stamps_s,
            traces,
            args.cal_median_log10_trace,
            args.cal_sigma_log10_trace,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    print_report(stats, args.bag_path, args.topic)

    if args.json:
        payload = {
            "bag_path": str(Path(args.bag_path).resolve()),
            "topic": args.topic,
            **stats,
        }
        with open(args.json, "w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2)
        print(f"\nResults saved -> {args.json}")


if __name__ == "__main__":
    main()
