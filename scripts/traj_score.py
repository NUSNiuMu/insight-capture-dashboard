#!/usr/bin/python3
"""
traj_score - trajectory quality evaluator for VIO / SLAM systems.

Reads PoseWithCovarianceStamped messages from a ROS2 bag, computes the
6x6 covariance trace for each pose, and summarises trajectory uncertainty
as a 0-100 quality score:

    score = min(100, ref_cov / max_trace * 100)

Higher score = tighter uncertainty = better VIO/SLAM performance.

Usage:
    traj_score <bag_path> [OPTIONS]

Requires:
    source /opt/ros/humble/setup.bash
"""

import argparse
import json
import math
import sys
from pathlib import Path

import rosbag2_py
from rclpy.serialization import deserialize_message
from rosidl_runtime_py.utilities import get_message


DEFAULT_TOPIC = "/insight7_a/camera/vio_image_cov"
DEFAULT_REF_COV = 1e-3

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


def compute_stats(traces: list, ref_cov: float) -> dict:
    n = len(traces)
    mean = sum(traces) / n
    std = math.sqrt(sum((x - mean) ** 2 for x in traces) / n)
    s = sorted(traces)
    score = min(100.0, ref_cov / s[-1] * 100.0)
    return {
        "n_poses": n,
        "mean_trace": mean,
        "std_trace": std,
        "min_trace": s[0],
        "max_trace": s[-1],
        "p50_trace": percentile(s, 50),
        "p90_trace": percentile(s, 90),
        "p99_trace": percentile(s, 99),
        "ref_cov": ref_cov,
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
) -> list:
    traces = []
    while reader.has_next():
        t_name, raw, _stamp = reader.read_next()
        if t_name != topic:
            continue
        msg = deserialize_message(raw, msg_type)
        trace = covariance_trace(list(msg.pose.covariance))
        traces.append(trace)
        if verbose:
            stamp = msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9
            print(f"  [{len(traces):6d}]  t={stamp:.3f}  trace={trace:.6e}")
    return traces


def print_report(stats: dict, bag_path: str, topic: str) -> None:
    width = 54
    print("=" * width)
    print("  Trajectory Quality Report")
    print(f"  Bag   : {bag_path}")
    print(f"  Topic : {topic}")
    print("-" * width)
    print(f"  Poses processed  : {stats['n_poses']}")
    print(f"  Mean cov trace   : {stats['mean_trace']:.6e}")
    print(f"  Std  cov trace   : {stats['std_trace']:.6e}")
    print(f"  Min  cov trace   : {stats['min_trace']:.6e}")
    print(f"  Max  cov trace   : {stats['max_trace']:.6e}")
    print(f"  p50  cov trace   : {stats['p50_trace']:.6e}")
    print(f"  p90  cov trace   : {stats['p90_trace']:.6e}")
    print(f"  p99  cov trace   : {stats['p99_trace']:.6e}")
    print("-" * width)
    print(f"  Reference cov    : {stats['ref_cov']:.6e}  (= score 100)")
    print(f"  Score            : {stats['score']:.1f} / 100  [{stats['quality']}]  (driven by max trace)")
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
        "--ref-cov",
        "-r",
        type=float,
        default=DEFAULT_REF_COV,
        help=(
            f"Covariance trace that maps to score 100 (default: {DEFAULT_REF_COV}). "
            "Tune to your system's expected best-case trace."
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

    traces = collect_traces(reader, args.topic, msg_type, args.verbose)
    if not traces:
        print("No messages found on this topic.", file=sys.stderr)
        sys.exit(1)

    stats = compute_stats(traces, args.ref_cov)
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
