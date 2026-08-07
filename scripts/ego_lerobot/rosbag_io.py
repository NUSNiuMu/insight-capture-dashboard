"""ROS bag scanning, interpolation, calibration, and selected-frame decoding."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import cv2
import numpy as np
from scipy.spatial.transform import Rotation, Slerp

from camera_setup import IMAGE_STREAMS, image_topic, load_setup
from hand_tracking.extract_gripper import decode_color_image


@dataclass(frozen=True)
class CameraStream:
    name: str
    role: str
    image_topic: str
    camera_info_topic: str
    pose_topic: str


@dataclass
class Timeline:
    all_image_stamps: dict[str, np.ndarray]
    selected_source_indices: dict[str, np.ndarray]
    selected_source_stamps: dict[str, np.ndarray]
    selected_valid: dict[str, np.ndarray]
    target_stamps: np.ndarray
    crop_actual_start_s: float
    crop_actual_end_s: float


def load_camera_streams(config_path: Path) -> dict[str, CameraStream]:
    setup = load_setup(config_path)
    by_role = {
        str(item.get("teleop_role")): item
        for item in setup.get("cameras", [])
        if item.get("enabled", True)
    }
    required = ("head", "left_hand", "right_hand")
    missing = [role for role in required if role not in by_role]
    if missing:
        raise ValueError(f"camera config is missing roles: {', '.join(missing)}")
    streams = {}
    for role in required:
        item = by_role[role]
        namespace = str(item["namespace"])
        stream = str(item["dashboard_image_stream"])
        if stream not in IMAGE_STREAMS:
            raise ValueError(f"unsupported stream {stream!r} for {namespace}")
        sensor = "color" if stream.startswith("color") else "infra1"
        streams[role] = CameraStream(
            name=str(item["name"]),
            role=role,
            image_topic=image_topic(namespace, stream),
            camera_info_topic=f"/{namespace}/camera/{sensor}/camera_info",
            pose_topic=str(item["dashboard_pose_stream"]),
        )
    return streams


def header_stamp_ns(message) -> int:
    stamp = message.header.stamp
    return int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)


def _reader(path: Path):
    from rosbags.highlevel import AnyReader
    from rosbags.typesys import Stores, get_typestore

    return AnyReader([path], default_typestore=get_typestore(Stores.ROS2_HUMBLE))


def load_topic_stamps(bag: Path, topic: str) -> np.ndarray:
    stamps = []
    with _reader(bag) as reader:
        connections = [item for item in reader.connections if item.topic == topic]
        if not connections:
            raise ValueError(f"missing image topic: {topic}")
        for connection, _record_stamp, raw in reader.messages(connections=connections):
            stamps.append(header_stamp_ns(reader.deserialize(raw, connection.msgtype)))
    result = np.asarray(stamps, dtype=np.int64)
    if result.size < 2 or not np.all(np.diff(result) > 0):
        raise ValueError(f"topic timestamps are not strictly increasing: {topic}")
    return result


def nearest_indices(source: np.ndarray, target: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    right = np.searchsorted(source, target, side="left")
    right = np.clip(right, 0, len(source) - 1)
    left = np.clip(right - 1, 0, len(source) - 1)
    choose_right = np.abs(source[right] - target) < np.abs(source[left] - target)
    indices = np.where(choose_right, right, left)
    gaps_ms = np.abs(source[indices] - target) / 1e6
    return indices.astype(np.int64), gaps_ms


def build_timeline(
    bag: Path,
    streams: dict[str, CameraStream],
    *,
    crop_start_s: float,
    crop_end_s: float,
    maximum_image_skew_ms: float,
) -> Timeline:
    all_stamps = {
        role: load_topic_stamps(bag, stream.image_topic)
        for role, stream in streams.items()
    }
    head = all_stamps["head"]
    start_ns = int(head[0] + round(crop_start_s * 1e9))
    end_ns = int(head[0] + round(crop_end_s * 1e9))
    start = int(np.searchsorted(head, start_ns, side="left"))
    stop = int(np.searchsorted(head, end_ns, side="right"))
    target = head[start:stop]
    if target.size < 2:
        raise ValueError("crop selected fewer than two head frames")
    indices = {"head": np.arange(start, stop, dtype=np.int64)}
    selected_stamps = {"head": target.copy()}
    selected_valid = {"head": np.ones(len(target), dtype=bool)}
    for role in ("left_hand", "right_hand"):
        selected, gaps = nearest_indices(all_stamps[role], target)
        indices[role] = selected
        selected_stamps[role] = all_stamps[role][selected]
        selected_valid[role] = gaps <= maximum_image_skew_ms
    return Timeline(
        all_image_stamps=all_stamps,
        selected_source_indices=indices,
        selected_source_stamps=selected_stamps,
        selected_valid=selected_valid,
        target_stamps=target,
        crop_actual_start_s=float((target[0] - head[0]) / 1e9),
        crop_actual_end_s=float((target[-1] - head[0]) / 1e9),
    )


def decode_image(message, message_type: str) -> np.ndarray | None:
    if message_type == "sensor_msgs/msg/CompressedImage":
        return cv2.imdecode(np.frombuffer(message.data, np.uint8), cv2.IMREAD_COLOR)
    return decode_color_image(message, message_type)


def selected_images(
    bag: Path,
    topic: str,
    source_indices: np.ndarray,
) -> Iterator[tuple[int, int, np.ndarray]]:
    wanted: dict[int, list[int]] = {}
    for target_index, source_index in enumerate(source_indices):
        wanted.setdefault(int(source_index), []).append(target_index)
    with _reader(bag) as reader:
        connections = [item for item in reader.connections if item.topic == topic]
        for source_index, (connection, _record_stamp, raw) in enumerate(
            reader.messages(connections=connections)
        ):
            targets = wanted.get(source_index)
            if targets is None:
                continue
            message = reader.deserialize(raw, connection.msgtype)
            frame = decode_image(message, connection.msgtype)
            if frame is None:
                raise RuntimeError(f"failed to decode {topic} frame {source_index}")
            stamp = header_stamp_ns(message)
            for target_index in targets:
                yield target_index, stamp, frame


def _message_pose(message) -> tuple[np.ndarray, np.ndarray]:
    pose = message.pose.pose if hasattr(message.pose, "pose") else message.pose
    return (
        np.asarray([pose.position.x, pose.position.y, pose.position.z], np.float64),
        np.asarray(
            [
                pose.orientation.x,
                pose.orientation.y,
                pose.orientation.z,
                pose.orientation.w,
            ],
            np.float64,
        ),
    )


def load_pose_samples(bag: Path, topic: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    stamps, positions, quaternions = [], [], []
    with _reader(bag) as reader:
        connections = [item for item in reader.connections if item.topic == topic]
        if not connections:
            raise ValueError(f"missing pose topic: {topic}")
        for connection, _record_stamp, raw in reader.messages(connections=connections):
            message = reader.deserialize(raw, connection.msgtype)
            position, quaternion = _message_pose(message)
            stamps.append(header_stamp_ns(message))
            positions.append(position)
            quaternions.append(quaternion)
    stamp_array = np.asarray(stamps, np.int64)
    order = np.argsort(stamp_array)
    return (
        stamp_array[order],
        np.asarray(positions, np.float64)[order],
        np.asarray(quaternions, np.float64)[order],
    )


def interpolate_pose(
    samples: tuple[np.ndarray, np.ndarray, np.ndarray],
    target_stamps: np.ndarray,
    *,
    maximum_bracket_gap_ms: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    stamps, positions, quaternions = samples
    right = np.searchsorted(stamps, target_stamps, side="left")
    left = right - 1
    in_range = (left >= 0) & (right < len(stamps))
    left = np.clip(left, 0, len(stamps) - 1)
    right = np.clip(right, 0, len(stamps) - 1)
    bracket_ms = (stamps[right] - stamps[left]) / 1e6
    valid = in_range & (bracket_ms <= maximum_bracket_gap_ms)
    denominator = np.maximum(stamps[right] - stamps[left], 1)
    alpha = np.clip((target_stamps - stamps[left]) / denominator, 0.0, 1.0)
    output_position = positions[left] + alpha[:, None] * (positions[right] - positions[left])
    output_quaternion = quaternions[left].copy()
    distinct = valid & (right != left)
    for frame_index in np.flatnonzero(distinct):
        rotation = Rotation.from_quat(quaternions[[left[frame_index], right[frame_index]]])
        output_quaternion[frame_index] = Slerp([0.0, 1.0], rotation)([alpha[frame_index]]).as_quat()[0]
    nearest_ms = np.minimum(
        np.abs(target_stamps - stamps[left]), np.abs(stamps[right] - target_stamps)
    ) / 1e6
    result = np.concatenate((output_position, output_quaternion), axis=1)
    result[~valid] = 0.0
    return result, valid, nearest_ms, bracket_ms


def load_camera_info(bag: Path, streams: dict[str, CameraStream]) -> dict[str, object]:
    result = {}
    with _reader(bag) as reader:
        by_topic = {stream.camera_info_topic: stream for stream in streams.values()}
        connections = [item for item in reader.connections if item.topic in by_topic]
        for connection, _stamp, raw in reader.messages(connections=connections):
            stream = by_topic[connection.topic]
            if stream.name in result:
                continue
            message = reader.deserialize(raw, connection.msgtype)
            result[stream.name] = {
                "topic": connection.topic,
                "frame_id": str(message.header.frame_id),
                "width": int(message.width),
                "height": int(message.height),
                "distortion_model": str(message.distortion_model),
                "distortion": [float(value) for value in message.d],
                "intrinsic": [float(value) for value in message.k],
                "rectification": [float(value) for value in message.r],
                "projection": [float(value) for value in message.p],
            }
            if len(result) == len(streams):
                break
    missing = [stream.name for stream in streams.values() if stream.name not in result]
    if missing:
        raise ValueError(f"missing CameraInfo for: {', '.join(missing)}")
    return result


def load_filtered_tf_static(bag: Path) -> list[dict[str, object]]:
    transforms = {}
    with _reader(bag) as reader:
        connections = [item for item in reader.connections if item.topic == "/tf_static"]
        for connection, _stamp, raw in reader.messages(connections=connections):
            message = reader.deserialize(raw, connection.msgtype)
            for item in message.transforms:
                parent, child = str(item.header.frame_id), str(item.child_frame_id)
                if "tcp" in parent.lower() or "tcp" in child.lower():
                    continue
                key = (parent, child)
                transforms[key] = {
                    "parent": parent,
                    "child": child,
                    "translation_m": [
                        float(item.transform.translation.x),
                        float(item.transform.translation.y),
                        float(item.transform.translation.z),
                    ],
                    "rotation_xyzw": [
                        float(item.transform.rotation.x),
                        float(item.transform.rotation.y),
                        float(item.transform.rotation.z),
                        float(item.transform.rotation.w),
                    ],
                }
    return [transforms[key] for key in sorted(transforms)]
