#!/usr/bin/env python3

"""Extract per-frame gripper marker distance and opening from a ROS 2 bag."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from hand_tracking.gripper import (
    DEFAULT_CALIBRATION_PATH,
    GripperCalibration,
    GripperMarkerDetector,
)


IMAGE_TYPES = {"sensor_msgs/msg/Image", "sensor_msgs/msg/CompressedImage"}
SCHEMA_VERSION = 1


def decode_image(message: object, message_type: str) -> Optional[np.ndarray]:
    """Decode supported ROS image messages into contiguous BGR or mono pixels."""

    if message_type == "sensor_msgs/msg/CompressedImage":
        return cv2.imdecode(
            np.frombuffer(message.data, dtype=np.uint8), cv2.IMREAD_COLOR
        )
    if message_type != "sensor_msgs/msg/Image":
        raise ValueError(f"unsupported image message type: {message_type}")

    height = int(message.height)
    width = int(message.width)
    step = int(message.step)
    if height <= 0 or width <= 0 or step <= 0:
        return None
    raw = np.frombuffer(message.data, dtype=np.uint8)
    encoding = str(message.encoding).lower()
    if encoding in {"mono8", "8uc1"}:
        if step < width or raw.size < height * step:
            return None
        return np.ascontiguousarray(
            raw[: height * step].reshape(height, step)[:, :width]
        )
    if encoding in {"bgr8", "rgb8"}:
        row_bytes = width * 3
        if step < row_bytes or raw.size < height * step:
            return None
        image = raw[: height * step].reshape(height, step)[:, :row_bytes]
        image = np.ascontiguousarray(image.reshape(height, width, 3))
        if encoding == "rgb8":
            return cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        return image
    if encoding in {"bgra8", "rgba8"}:
        row_bytes = width * 4
        if step < row_bytes or raw.size < height * step:
            return None
        image = raw[: height * step].reshape(height, step)[:, :row_bytes]
        image = np.ascontiguousarray(image.reshape(height, width, 4))
        conversion = (
            cv2.COLOR_BGRA2BGR if encoding == "bgra8" else cv2.COLOR_RGBA2BGR
        )
        return cv2.cvtColor(image, conversion)
    if encoding == "nv12":
        if step < width or raw.size < height * step:
            return None
        # Gripper detection only consumes luminance; ignore the interleaved UV plane.
        return np.ascontiguousarray(
            raw[: height * step].reshape(height, step)[:, :width]
        )
    raise ValueError(f"unsupported image encoding: {message.encoding}")


def header_stamp_ns(message: object) -> int:
    stamp = message.header.stamp
    return int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)


def infer_camera_name(topic: str) -> str:
    parts = [part for part in topic.split("/") if part]
    if not parts:
        raise ValueError(f"cannot infer camera name from topic '{topic}'")
    return parts[0]


def choose_image_topic(
    topics_and_types: dict[str, str],
    *,
    requested_topic: Optional[str],
    camera_name: Optional[str],
) -> tuple[str, str]:
    if requested_topic:
        message_type = topics_and_types.get(requested_topic)
        if message_type is None:
            raise ValueError(f"topic '{requested_topic}' is absent from the bag")
        if message_type not in IMAGE_TYPES:
            raise ValueError(
                f"topic '{requested_topic}' has unsupported type '{message_type}'"
            )
        return requested_topic, message_type

    candidates = [
        (name, message_type)
        for name, message_type in topics_and_types.items()
        if message_type in IMAGE_TYPES
        and (camera_name is None or f"/{camera_name}/" in name)
    ]
    preferred = [item for item in candidates if "image_rect_raw" in item[0]]
    candidates = preferred or candidates
    if len(candidates) != 1:
        available = ", ".join(name for name, _ in candidates) or "none"
        raise ValueError(
            "could not choose one image topic; pass --topic or --camera "
            f"(candidates: {available})"
        )
    return candidates[0]


def load_calibration(
    calibration_path: Path,
    camera_name: str,
    *,
    open_px: Optional[float],
    closed_px: Optional[float],
) -> GripperCalibration:
    if (open_px is None) != (closed_px is None):
        raise ValueError("--open-px and --closed-px must be provided together")
    if open_px is not None and closed_px is not None:
        calibration = GripperCalibration(float(open_px), float(closed_px))
    else:
        payload = {}
        if calibration_path.is_file():
            payload = json.loads(calibration_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("gripper calibration must be a JSON object")
        entry = payload.get(camera_name, {})
        if not isinstance(entry, dict):
            raise ValueError(f"calibration for '{camera_name}' must be a JSON object")
        entry_open_px = entry.get("open_px")
        entry_closed_px = entry.get("closed_px")
        if entry_open_px is None and entry_closed_px is None:
            calibration = GripperCalibration()
        elif entry_open_px is None or entry_closed_px is None:
            raise ValueError(f"incomplete gripper calibration for '{camera_name}'")
        else:
            try:
                calibration = GripperCalibration(
                    float(entry_open_px), float(entry_closed_px)
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"non-numeric gripper calibration for '{camera_name}'"
                ) from exc
    if (
        calibration.open_px is not None or calibration.closed_px is not None
    ) and not calibration.is_valid:
        raise ValueError(f"invalid gripper calibration for '{camera_name}'")
    return calibration


def extract_gripper(
    bag_path: Path,
    output_path: Path,
    *,
    topic: Optional[str] = None,
    camera_name: Optional[str] = None,
    calibration_path: Path = Path(DEFAULT_CALIBRATION_PATH),
    open_px: Optional[float] = None,
    closed_px: Optional[float] = None,
    require_calibration: bool = False,
) -> dict[str, object]:
    """Extract one JSON-ready record per image in a bag."""

    try:
        import rosbag2_py
        from rclpy.serialization import deserialize_message
        from rosidl_runtime_py.utilities import get_message
    except ImportError as exc:
        raise RuntimeError("ROS 2 rosbag Python dependencies are unavailable") from exc

    bag_path = Path(bag_path).resolve()
    if bag_path.is_file() and bag_path.suffix == ".db3":
        bag_path = bag_path.parent
    if not bag_path.is_dir():
        raise ValueError(f"bag directory does not exist: {bag_path}")

    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(bag_path), storage_id=""),
        rosbag2_py.ConverterOptions(
            input_serialization_format="cdr",
            output_serialization_format="cdr",
        ),
    )
    topics_and_types = {
        item.name: item.type for item in reader.get_all_topics_and_types()
    }
    selected_topic, message_type = choose_image_topic(
        topics_and_types,
        requested_topic=topic,
        camera_name=camera_name,
    )
    camera_name = camera_name or infer_camera_name(selected_topic)
    calibration = load_calibration(
        Path(calibration_path),
        camera_name,
        open_px=open_px,
        closed_px=closed_px,
    )
    if require_calibration and not calibration.is_valid:
        raise ValueError(
            f"no valid calibration for '{camera_name}' in {calibration_path}"
        )

    reader.set_filter(rosbag2_py.StorageFilter(topics=[selected_topic]))
    ros_message_type = get_message(message_type)
    detector = GripperMarkerDetector()
    frames = []
    decoded_frames = 0
    both_detected_frames = 0
    left_detected_frames = 0
    right_detected_frames = 0
    started = time.perf_counter()
    while reader.has_next():
        current_topic, raw, record_stamp_ns = reader.read_next()
        if current_topic != selected_topic:
            continue
        message = deserialize_message(raw, ros_message_type)
        image = decode_image(message, message_type)
        result = detector.detect(image) if image is not None else None
        decoded_frames += int(image is not None)
        left_found = result is not None and result.left_center_px is not None
        right_found = result is not None and result.right_center_px is not None
        both_found = left_found and right_found
        left_detected_frames += int(left_found)
        right_detected_frames += int(right_found)
        both_detected_frames += int(both_found)
        distance_px = None if result is None else result.distance_px
        opening = (
            calibration.normalize(distance_px)
            if distance_px is not None and calibration.is_valid
            else None
        )
        frames.append(
            {
                "frame_index": len(frames),
                "record_stamp_ns": int(record_stamp_ns),
                "header_stamp_ns": header_stamp_ns(message),
                "left_center_px": (
                    None if not left_found else list(result.left_center_px)
                ),
                "right_center_px": (
                    None if not right_found else list(result.right_center_px)
                ),
                "distance_px": distance_px,
                "opening": opening,
            }
        )
        if len(frames) % 100 == 0:
            print(
                f"GRIPPER_PROGRESS {len(frames)} {both_detected_frames}",
                flush=True,
            )

    total_frames = len(frames)
    elapsed_s = time.perf_counter() - started
    distances_px = [
        float(frame["distance_px"])
        for frame in frames
        if frame["distance_px"] is not None
    ]
    first_header_stamp = frames[0]["header_stamp_ns"] if frames else None
    last_header_stamp = frames[-1]["header_stamp_ns"] if frames else None
    summary = {
        "total_frames": total_frames,
        "decoded_frames": decoded_frames,
        "left_detected_frames": left_detected_frames,
        "right_detected_frames": right_detected_frames,
        "both_detected_frames": both_detected_frames,
        "left_detection_rate": (
            round(left_detected_frames / total_frames, 6) if total_frames else 0.0
        ),
        "right_detection_rate": (
            round(right_detected_frames / total_frames, 6) if total_frames else 0.0
        ),
        "both_detection_rate": (
            round(both_detected_frames / total_frames, 6) if total_frames else 0.0
        ),
        "distance_px_min": min(distances_px) if distances_px else None,
        "distance_px_median": (
            float(np.median(distances_px)) if distances_px else None
        ),
        "distance_px_max": max(distances_px) if distances_px else None,
        "first_header_stamp_ns": first_header_stamp,
        "last_header_stamp_ns": last_header_stamp,
        "header_duration_s": (
            round((last_header_stamp - first_header_stamp) / 1e9, 6)
            if first_header_stamp is not None and last_header_stamp is not None
            else 0.0
        ),
        "processing_seconds": round(elapsed_s, 3),
        "processing_fps": (
            round(total_frames / elapsed_s, 2) if elapsed_s > 0 else None
        ),
    }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "bag_path": str(bag_path),
            "topic": selected_topic,
            "message_type": message_type,
            "camera": camera_name,
        },
        "calibration": {
            "path": str(Path(calibration_path)),
            "valid": calibration.is_valid,
            "open_px": calibration.open_px,
            "closed_px": calibration.closed_px,
            "opening_convention": "0=closed,1=open",
        },
        "summary": summary,
        "frames": frames,
    }
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pending_path = output_path.with_suffix(output_path.suffix + ".pending")
    pending_path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    pending_path.replace(output_path)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bag", type=Path, help="ROS 2 bag directory or .db3 file")
    parser.add_argument("--topic", help="image topic; auto-selected when unambiguous")
    parser.add_argument(
        "--camera", help="camera name used for topic and calibration selection"
    )
    parser.add_argument("--output", type=Path, help="output JSON path")
    parser.add_argument(
        "--calibration",
        type=Path,
        default=Path(DEFAULT_CALIBRATION_PATH),
        help="gripper calibration JSON",
    )
    parser.add_argument(
        "--open-px", type=float, help="override calibrated open distance"
    )
    parser.add_argument(
        "--closed-px", type=float, help="override calibrated closed distance"
    )
    parser.add_argument(
        "--require-calibration",
        action="store_true",
        help="fail instead of emitting opening=null when calibration is absent",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    bag_path = args.bag.resolve()
    bag_name = bag_path.parent.name if bag_path.suffix == ".db3" else bag_path.name
    camera_name = args.camera
    output_path = args.output
    if output_path is None:
        camera_label = camera_name or "auto"
        project_root = Path(__file__).resolve().parents[2]
        output_path = (
            project_root
            / "outputs"
            / "gripper"
            / bag_name
            / f"{camera_label}.json"
        )
    try:
        payload = extract_gripper(
            bag_path,
            output_path,
            topic=args.topic,
            camera_name=camera_name,
            calibration_path=args.calibration,
            open_px=args.open_px,
            closed_px=args.closed_px,
            require_calibration=args.require_calibration,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    # Rename the auto-selected output once the camera is known.
    if args.output is None and camera_name is None:
        selected_camera = str(payload["source"]["camera"])
        final_path = output_path.with_name(f"{selected_camera}.json")
        output_path.replace(final_path)
        output_path = final_path
    print(
        f"GRIPPER_DONE {payload['summary']['total_frames']} "
        f"{payload['summary']['both_detected_frames']} {output_path}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
