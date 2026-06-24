#!/usr/bin/env python3

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import rosbag2_py
from rclpy.serialization import deserialize_message
from rosidl_runtime_py.utilities import get_message


def stamp_to_ns(stamp) -> int:
    return int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)


def message_stamp_ns(msg, fallback_ns: int) -> int:
    header = getattr(msg, "header", None)
    stamp = getattr(header, "stamp", None)
    if stamp is None:
        return fallback_ns
    return stamp_to_ns(stamp)


def quaternion_angle_deg(start_xyzw: Tuple[float, float, float, float], end_xyzw: Tuple[float, float, float, float]) -> float:
    dot = abs(sum(a * b for a, b in zip(normalize_quaternion(start_xyzw), normalize_quaternion(end_xyzw))))
    dot = min(1.0, max(-1.0, dot))
    return math.degrees(2.0 * math.acos(dot))


def normalize_quaternion(quat: Tuple[float, float, float, float]) -> Tuple[float, float, float, float]:
    norm = math.sqrt(sum(value * value for value in quat))
    if norm <= 0.0:
        return (0.0, 0.0, 0.0, 1.0)
    return tuple(value / norm for value in quat)  # type: ignore[return-value]


def open_reader(bag_path: Path, storage_id: str) -> rosbag2_py.SequentialReader:
    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(bag_path), storage_id=storage_id),
        rosbag2_py.ConverterOptions("", ""),
    )
    return reader


def infer_storage_id(bag_path: Path) -> str:
    if bag_path.is_file():
        if bag_path.suffix == ".mcap":
            return "mcap"
        if bag_path.suffix == ".db3":
            return "sqlite3"
    metadata = bag_path / "metadata.yaml"
    if metadata.exists():
        for line in metadata.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("storage_identifier:"):
                return stripped.split(":", 1)[1].strip()
    return "sqlite3"


def resolve_bag_path(bag_path: Path) -> Path:
    if bag_path.is_file():
        return bag_path
    bag_files = sorted([*bag_path.glob("*.mcap"), *bag_path.glob("*.db3")])
    if len(bag_files) == 1:
        # Some generated metadata can reference a stale filename; read the real
        # storage file directly in that case.
        return bag_files[0]
    return bag_path


def pick_vio_topics(topic_types: Dict[str, str], requested: Optional[str]) -> List[str]:
    if requested:
        if requested not in topic_types:
            raise RuntimeError(f"requested VIO topic not found: {requested}")
        return [requested]
    candidates = [
        topic for topic, type_name in topic_types.items()
        if type_name == "geometry_msgs/msg/PoseStamped" and ("vio" in topic or "pose" in topic)
    ]
    if not candidates:
        candidates = [
            topic for topic, type_name in topic_types.items()
            if type_name == "geometry_msgs/msg/PoseStamped"
        ]
    if not candidates:
        raise RuntimeError("no PoseStamped VIO topic found")
    return sorted(candidates)


def summarize_trajectory(
    positions: List[Tuple[float, float, float]],
    quaternions: List[Tuple[float, float, float, float]],
    closure_ratio_threshold: float,
) -> Dict[str, object]:
    if len(positions) < 2:
        raise RuntimeError("not enough VIO poses")

    segment_lengths = [math.dist(a, b) for a, b in zip(positions, positions[1:])]
    trajectory_length = sum(segment_lengths)
    start_to_end_distance = math.dist(positions[0], positions[-1])
    closure_ratio = start_to_end_distance / trajectory_length if trajectory_length > 0 else float("inf")

    return {
        "pose_count": len(positions),
        "trajectory_length_m": trajectory_length,
        "start_to_end_distance_m": start_to_end_distance,
        "closure_ratio": closure_ratio,
        "closure_ratio_percent": closure_ratio * 100.0,
        "closure_pass": closure_ratio <= closure_ratio_threshold,
        "angle_change_deg": quaternion_angle_deg(quaternions[0], quaternions[-1]),
        "start_position": positions[0],
        "end_position": positions[-1],
        "step_median_mm": sorted(segment_lengths)[len(segment_lengths) // 2] * 1000.0,
        "step_max_mm": max(segment_lengths) * 1000.0,
    }


def summarize_timing(stamps: List[int], expected_hz: Optional[float] = None) -> Dict[str, object]:
    if len(stamps) < 2:
        return {
            "count": len(stamps),
            "duration_sec": 0.0,
            "hz": 0.0,
            "median_dt_ms": None,
            "p95_dt_ms": None,
            "max_dt_ms": None,
            "gap_count": 0,
            "estimated_missed": 0,
            "nonmonotonic_count": 0,
        }
    dts = [(b - a) / 1e9 for a, b in zip(stamps, stamps[1:])]
    duration = (stamps[-1] - stamps[0]) / 1e9
    sorted_dts = sorted(dts)
    median = sorted_dts[len(sorted_dts) // 2]
    expected_dt = (1.0 / expected_hz) if expected_hz and expected_hz > 0 else median
    gap_threshold = expected_dt * 1.5
    gaps = [dt for dt in dts if dt > gap_threshold]
    missed = sum(max(0, round(dt / expected_dt) - 1) for dt in gaps if expected_dt > 0)
    return {
        "count": len(stamps),
        "duration_sec": duration,
        "hz": (len(stamps) - 1) / duration if duration > 0 else 0.0,
        "median_dt_ms": median * 1000.0,
        "p95_dt_ms": sorted_dts[int(len(sorted_dts) * 0.95)] * 1000.0,
        "max_dt_ms": max(dts) * 1000.0,
        "gap_threshold_ms": gap_threshold * 1000.0,
        "gap_count": len(gaps),
        "estimated_missed": missed,
        "nonmonotonic_count": sum(1 for dt in dts if dt <= 0),
    }


def evaluate_bag(
    bag_path: Path,
    storage_id: str,
    vio_topic: Optional[str],
    closure_ratio_threshold: float,
) -> Dict[str, object]:
    reader = open_reader(bag_path, storage_id)
    topic_types = {topic.name: topic.type for topic in reader.get_all_topics_and_types()}
    selected_vio_topics = pick_vio_topics(topic_types, vio_topic)
    selected_vio_topic_set = set(selected_vio_topics)
    message_classes = {topic: get_message(type_name) for topic, type_name in topic_types.items()}

    stamps_by_topic: Dict[str, List[int]] = defaultdict(list)
    positions_by_topic: Dict[str, List[Tuple[float, float, float]]] = defaultdict(list)
    quaternions_by_topic: Dict[str, List[Tuple[float, float, float, float]]] = defaultdict(list)
    vio_status_values: List[str] = []

    while reader.has_next():
        topic, data, bag_ts = reader.read_next()
        msg = deserialize_message(data, message_classes[topic])
        if topic in selected_vio_topic_set:
            stamps_by_topic[topic].append(message_stamp_ns(msg, bag_ts))
            p = msg.pose.position
            q = msg.pose.orientation
            positions_by_topic[topic].append((float(p.x), float(p.y), float(p.z)))
            quaternions_by_topic[topic].append((float(q.x), float(q.y), float(q.z), float(q.w)))
        elif topic.endswith("vio_status"):
            vio_status_values.append(str(getattr(msg, "data", "")))
        elif "image" in topic or topic.endswith("camera_info") or topic.endswith("imu"):
            stamps_by_topic[topic].append(message_stamp_ns(msg, bag_ts))

    vio_results = {}
    for topic in selected_vio_topics:
        if len(positions_by_topic[topic]) >= 2:
            vio_results[topic] = summarize_trajectory(
                positions_by_topic[topic],
                quaternions_by_topic[topic],
                closure_ratio_threshold,
            )
    if not vio_results:
        raise RuntimeError(f"not enough VIO poses on selected topic(s): {selected_vio_topics}")

    topic_timing = {}
    for topic, stamps in sorted(stamps_by_topic.items()):
        type_name = topic_types.get(topic, "")
        expected_hz = 100.0 if topic in selected_vio_topic_set else None
        if type_name == "sensor_msgs/msg/Imu":
            expected_hz = 400.0
        elif "image" in topic or topic.endswith("camera_info"):
            expected_hz = 30.0
        topic_timing[topic] = summarize_timing(stamps, expected_hz=expected_hz)

    return {
        "bag": str(bag_path),
        "storage_id": storage_id,
        "vio_topics": selected_vio_topics,
        "closure_ratio_threshold": closure_ratio_threshold,
        "closure_pass": all(vio["closure_pass"] for vio in vio_results.values()),
        "vios": vio_results,
        "topics": topic_timing,
        "vio_status": dict(Counter(vio_status_values)),
    }


def print_report(result: Dict[str, object]) -> None:
    problematic_topics = [
        topic for topic, stats in result["topics"].items()
        if stats["gap_count"] or stats["estimated_missed"] or stats["nonmonotonic_count"]
    ]

    print(f"Bag: {result['bag']}")
    print(f"VIO topics: {len(result['vios'])}")
    print(f"Loop closure: {'PASS' if result['closure_pass'] else 'FAIL'}")
    print(f"Dropped frames: {'CHECK' if problematic_topics else 'PASS'} ({len(problematic_topics)} topic(s) with gaps/nonmonotonic stamps)")
    for topic, trajectory in result["vios"].items():
        print(f"\n[{topic}]")
        print(
            "VIO drift: "
            f"{'PASS' if trajectory['closure_pass'] else 'FAIL'}, "
            f"length={trajectory['trajectory_length_m']:.4f} m, "
            f"start_end={trajectory['start_to_end_distance_m']:.4f} m, "
            f"ratio={trajectory['closure_ratio']:.6f} "
            f"({trajectory['closure_ratio_percent']:.3f}%), "
            f"threshold={result['closure_ratio_threshold']:.6f}"
        )
        print(f"Angle change: {trajectory['angle_change_deg']:.2f} deg")
        print(
            "Step: "
            f"median={trajectory['step_median_mm']:.2f} mm, "
            f"max={trajectory['step_max_mm']:.2f} mm"
        )
        print(f"Start position: {format_xyz(trajectory['start_position'])}")
        print(f"End position: {format_xyz(trajectory['end_position'])}")
    print()
    print("Topic timing:")
    for topic, stats in result["topics"].items():
        print(
            f"  {topic}: count={stats['count']} hz={stats['hz']:.2f} "
            f"median={format_ms(stats['median_dt_ms'])} "
            f"p95={format_ms(stats['p95_dt_ms'])} "
            f"max={format_ms(stats['max_dt_ms'])} "
            f"gaps={stats['gap_count']} missed~{stats['estimated_missed']} "
            f"nonmono={stats['nonmonotonic_count']}"
        )
    if result["vio_status"]:
        print(f"VIO status: {result['vio_status']}")


def format_ms(value: object) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.2f}ms"


def format_xyz(value: object) -> str:
    x, y, z = value  # type: ignore[misc]
    return f"({float(x):.4f}, {float(y):.4f}, {float(z):.4f})"


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate rosbag timing and VIO loop-closure quality.")
    parser.add_argument("bag", help="Rosbag directory or single .db3/.mcap file")
    parser.add_argument("--storage-id", default="", help="Storage id, inferred by default")
    parser.add_argument("--vio-topic", default="", help="PoseStamped VIO topic, auto-detected by default")
    parser.add_argument(
        "--closure-ratio-threshold",
        type=float,
        default=0.005,
        help="Pass threshold for start-end distance / trajectory length. Default: 0.005",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text report")
    args = parser.parse_args()

    bag_path = Path(args.bag).resolve()
    if not bag_path.exists():
        raise FileNotFoundError(f"bag does not exist: {bag_path}")
    read_path = resolve_bag_path(bag_path)
    storage_id = args.storage_id or infer_storage_id(read_path)
    result = evaluate_bag(
        read_path,
        storage_id=storage_id,
        vio_topic=args.vio_topic or None,
        closure_ratio_threshold=args.closure_ratio_threshold,
    )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print_report(result)


if __name__ == "__main__":
    main()
