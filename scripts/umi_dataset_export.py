#!/usr/bin/env python3

"""Convert recorded ROS 2 bags into an official-style UMI Zarr."""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
import time
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional

import cv2
import numpy as np
from scipy.spatial.transform import Rotation, Slerp

from camera_setup import IMAGE_STREAMS, image_topic, load_setup
from hand_tracking.extract_gripper import decode_image, load_calibration
from hand_tracking.gripper import GripperMarkerDetector


IMAGE_TYPES = {"sensor_msgs/msg/Image", "sensor_msgs/msg/CompressedImage"}
POSE_TYPES = {
    "geometry_msgs/msg/PoseStamped",
    "geometry_msgs/msg/PoseWithCovarianceStamped",
}
ROLE_ORDER = ("right_hand", "left_hand", "head")
SCHEMA_VERSION = 2
DEFAULT_MAX_POSITION_STEP_M = 0.05
DEFAULT_MAX_ORIENTATION_STEP_DEG = 45.0
DEFAULT_MAX_POSE_GAP_MS = 100.0


@dataclass(frozen=True)
class CameraSpec:
    name: str
    role: str
    image_topic: str
    pose_topic: Optional[str]
    tcp_translation_m: Optional[np.ndarray]
    tcp_rotation_xyzw: Optional[np.ndarray]


@dataclass
class StreamScan:
    image_stamps: Dict[str, np.ndarray]
    image_shapes: Dict[str, tuple[int, int]]
    poses: Dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]
    openings: Dict[str, tuple[np.ndarray, np.ndarray]]
    detection_rates: Dict[str, float]
    topic_types: Dict[str, str]


@dataclass
class EpisodePlan:
    bag_name: str
    timestamps_ns: np.ndarray
    image_indices: Dict[str, np.ndarray]
    lowdim: Dict[str, np.ndarray]
    detection_rates: Dict[str, float]
    max_image_skew_ms: float
    pose_quality_events: Dict[str, Dict[str, int]]


class ZarrV2Writer:
    """Write the subset of Zarr v2 used by UMI without a runtime dependency."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.create_group("")

    def create_group(self, name: str, attrs: Optional[dict] = None) -> None:
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        (path / ".zgroup").write_text('{"zarr_format":2}', encoding="utf-8")
        if attrs is not None:
            (path / ".zattrs").write_text(
                json.dumps(attrs, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )

    def create_array(
        self,
        name: str,
        shape: tuple[int, ...],
        chunks: tuple[int, ...],
        dtype,
        *,
        compression_level: int,
    ) -> None:
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        metadata = {
            "zarr_format": 2,
            "shape": list(shape),
            "chunks": list(chunks),
            "dtype": np.dtype(dtype).str,
            "compressor": {"id": "zlib", "level": int(compression_level)},
            "fill_value": 0,
            "order": "C",
            "filters": None,
        }
        (path / ".zarray").write_text(
            json.dumps(metadata, separators=(",", ":")), encoding="utf-8"
        )
        (path / ".zattrs").write_text("{}", encoding="utf-8")

    def write_frame(self, name: str, index: int, value: np.ndarray) -> None:
        value = np.ascontiguousarray(value)
        key = ".".join([str(int(index))] + ["0"] * value.ndim)
        (self.root / name / key).write_bytes(zlib.compress(value.tobytes(), level=1))

    def write_array(
        self,
        name: str,
        values: np.ndarray,
        *,
        chunk_length: int = 1024,
        compression_level: int = 5,
    ) -> None:
        values = np.ascontiguousarray(values)
        chunks = (min(max(len(values), 1), chunk_length),) + values.shape[1:]
        self.create_array(
            name,
            values.shape,
            chunks,
            values.dtype,
            compression_level=compression_level,
        )
        for chunk_index, start in enumerate(range(0, len(values), chunks[0])):
            chunk = np.ascontiguousarray(values[start : start + chunks[0]])
            key = ".".join([str(chunk_index)] + ["0"] * (values.ndim - 1))
            (self.root / name / key).write_bytes(
                zlib.compress(chunk.tobytes(), level=compression_level)
            )

    def set_root_attrs(self, attrs: dict) -> None:
        (self.root / ".zattrs").write_text(
            json.dumps(attrs, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )


def _pose_topic(camera: dict, *, single_arm: bool = False) -> str:
    key = "umi_single_arm_pose_stream" if single_arm else "dashboard_pose_stream"
    value = str(camera.get(key, "vio_100hz")).strip()
    if value.startswith("/"):
        return value
    return f"/{camera['namespace']}/camera/{value}"


def load_camera_specs(
    config_path: Path, camera_names: Optional[list[str]] = None
) -> list[CameraSpec]:
    setup = load_setup(config_path)
    enabled = [
        item for item in setup.get("cameras", []) if item.get("enabled", True)
    ]
    by_role = {
        str(item.get("teleop_role")): item
        for item in enabled
    }
    if camera_names is None:
        missing = [role for role in ROLE_ORDER if role not in by_role]
        if missing:
            raise ValueError(f"camera config is missing roles: {', '.join(missing)}")
        selected_roles = list(ROLE_ORDER)
    else:
        if not camera_names:
            raise ValueError("select at least one camera")
        if len(set(camera_names)) != len(camera_names):
            raise ValueError("duplicate camera names are not allowed")
        by_name = {str(item.get("name")): item for item in enabled}
        missing = [name for name in camera_names if name not in by_name]
        if missing:
            raise ValueError(f"camera config is missing cameras: {', '.join(missing)}")
        selected_by_role = {
            str(by_name[name].get("teleop_role")): by_name[name]
            for name in camera_names
        }
        if len(selected_by_role) != len(camera_names):
            raise ValueError("selected cameras must have unique teleoperation roles")
        selected_roles = [role for role in ROLE_ORDER if role in selected_by_role]
        if not any(role != "head" for role in selected_roles):
            raise ValueError("UMI export requires at least one hand camera")
        by_role = selected_by_role
    specs = []
    single_arm = sum(role != "head" for role in selected_roles) == 1
    for role in selected_roles:
        camera = by_role[role]
        stream = str(camera["dashboard_image_stream"])
        if stream not in IMAGE_STREAMS:
            raise ValueError(f"unsupported image stream '{stream}' for {camera['name']}")
        tcp = camera.get("camera_center_to_tcp") if role != "head" else None
        if role != "head" and not isinstance(tcp, dict):
            raise ValueError(f"{camera['name']} has no camera_center_to_tcp calibration")
        specs.append(
            CameraSpec(
                name=str(camera["name"]),
                role=role,
                image_topic=image_topic(str(camera["namespace"]), stream),
                pose_topic=(
                    _pose_topic(camera, single_arm=single_arm)
                    if role != "head"
                    else None
                ),
                tcp_translation_m=(
                    np.asarray(tcp["translation_m"], dtype=np.float64)
                    if tcp is not None
                    else None
                ),
                tcp_rotation_xyzw=(
                    np.asarray(tcp["rotation_xyzw"], dtype=np.float64)
                    if tcp is not None
                    else None
                ),
            )
        )
    return specs


def _open_reader(bag_path: Path, topics: Iterable[str]):
    import rosbag2_py

    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(bag_path), storage_id=""),
        rosbag2_py.ConverterOptions(
            input_serialization_format="cdr", output_serialization_format="cdr"
        ),
    )
    topic_types = {item.name: item.type for item in reader.get_all_topics_and_types()}
    reader.set_filter(rosbag2_py.StorageFilter(topics=list(topics)))
    return reader, topic_types


def _message_pose(message: object) -> tuple[np.ndarray, np.ndarray]:
    pose = message.pose.pose if hasattr(message.pose, "pose") else message.pose
    position = np.array(
        [pose.position.x, pose.position.y, pose.position.z], dtype=np.float64
    )
    quaternion = np.array(
        [
            pose.orientation.x,
            pose.orientation.y,
            pose.orientation.z,
            pose.orientation.w,
        ],
        dtype=np.float64,
    )
    norm = float(np.linalg.norm(quaternion))
    if not np.isfinite(norm) or norm <= 1e-9:
        raise ValueError("pose contains an invalid quaternion")
    return position, quaternion / norm


def scan_bag(
    bag_path: Path,
    specs: list[CameraSpec],
    calibration_path: Path,
) -> StreamScan:
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_message

    topics = {spec.image_topic for spec in specs}
    topics.update(spec.pose_topic for spec in specs if spec.pose_topic)
    reader, topic_types = _open_reader(bag_path, topics)
    missing = sorted(topic for topic in topics if topic not in topic_types)
    if missing:
        raise ValueError(f"missing required topics: {', '.join(missing)}")
    for spec in specs:
        if topic_types[spec.image_topic] not in IMAGE_TYPES:
            raise ValueError(f"{spec.image_topic} is not a supported image topic")
        if spec.pose_topic and topic_types[spec.pose_topic] not in POSE_TYPES:
            raise ValueError(f"{spec.pose_topic} is not a supported pose topic")

    message_classes = {
        topic: get_message(topic_types[topic]) for topic in topics
    }
    image_stamps: Dict[str, list[int]] = {spec.name: [] for spec in specs}
    image_shapes: Dict[str, tuple[int, int]] = {}
    pose_stamps: Dict[str, list[int]] = {
        spec.name: [] for spec in specs if spec.pose_topic
    }
    pose_positions: Dict[str, list[np.ndarray]] = {
        spec.name: [] for spec in specs if spec.pose_topic
    }
    pose_quaternions: Dict[str, list[np.ndarray]] = {
        spec.name: [] for spec in specs if spec.pose_topic
    }
    opening_stamps: Dict[str, list[int]] = {
        spec.name: [] for spec in specs if spec.role != "head"
    }
    opening_values: Dict[str, list[float]] = {
        spec.name: [] for spec in specs if spec.role != "head"
    }
    detectors = {
        spec.name: GripperMarkerDetector() for spec in specs if spec.role != "head"
    }
    calibrations = {
        spec.name: load_calibration(
            calibration_path,
            spec.name,
            open_px=None,
            closed_px=None,
        )
        for spec in specs
        if spec.role != "head"
    }
    invalid_calibrations = [name for name, value in calibrations.items() if not value.is_valid]
    if invalid_calibrations:
        raise ValueError(
            f"missing gripper calibration: {', '.join(invalid_calibrations)}"
        )
    missing_metric_calibrations = [
        name for name, value in calibrations.items() if not value.has_metric_width
    ]
    if missing_metric_calibrations:
        raise ValueError(
            "missing metric gripper width calibration for "
            f"{', '.join(missing_metric_calibrations)}; add at least two measured "
            "width_calibration points with distance_px and width_m"
        )

    image_by_topic = {spec.image_topic: spec for spec in specs}
    pose_by_topic = {spec.pose_topic: spec for spec in specs if spec.pose_topic}
    while reader.has_next():
        topic, raw, record_stamp_ns = reader.read_next()
        message = deserialize_message(raw, message_classes[topic])
        if topic in image_by_topic:
            spec = image_by_topic[topic]
            image_stamps[spec.name].append(int(record_stamp_ns))
            image = None
            if spec.role != "head" or spec.name not in image_shapes:
                image = decode_image(message, topic_types[topic])
                if image is None:
                    raise ValueError(f"failed to decode the first {spec.name} image")
                shape = (int(image.shape[0]), int(image.shape[1]))
                previous_shape = image_shapes.setdefault(spec.name, shape)
                if previous_shape != shape:
                    raise ValueError(
                        f"{spec.name} image resolution changed from "
                        f"{previous_shape[1]}x{previous_shape[0]} to {shape[1]}x{shape[0]}"
                    )
            if spec.role != "head":
                result = detectors[spec.name].detect(image) if image is not None else None
                distance = result.distance_px if result is not None else None
                if distance is not None:
                    opening_stamps[spec.name].append(int(record_stamp_ns))
                    width_m = calibrations[spec.name].width_m(distance)
                    assert width_m is not None
                    opening_values[spec.name].append(width_m)
        elif topic in pose_by_topic:
            spec = pose_by_topic[topic]
            position, quaternion = _message_pose(message)
            pose_stamps[spec.name].append(int(record_stamp_ns))
            pose_positions[spec.name].append(position)
            pose_quaternions[spec.name].append(quaternion)

    image_arrays = {
        name: np.asarray(values, dtype=np.int64) for name, values in image_stamps.items()
    }
    poses = {
        name: (
            np.asarray(pose_stamps[name], dtype=np.int64),
            np.asarray(pose_positions[name], dtype=np.float64),
            np.asarray(pose_quaternions[name], dtype=np.float64),
        )
        for name in pose_stamps
    }
    openings = {
        name: (
            np.asarray(opening_stamps[name], dtype=np.int64),
            np.asarray(opening_values[name], dtype=np.float64),
        )
        for name in opening_stamps
    }
    for name, stamps in image_arrays.items():
        if stamps.size < 2:
            raise ValueError(f"{name} has fewer than two image frames")
    for name, (stamps, _, _) in poses.items():
        if stamps.size < 2:
            raise ValueError(f"{name} has fewer than two poses")
    for name, (stamps, _) in openings.items():
        if stamps.size < 2:
            raise ValueError(f"{name} has fewer than two dual-marker detections")
    detection_rates = {
        name: round(len(openings[name][0]) / len(image_arrays[name]), 6)
        for name in openings
    }
    return StreamScan(
        image_stamps=image_arrays,
        image_shapes=image_shapes,
        poses=poses,
        openings=openings,
        detection_rates=detection_rates,
        topic_types=topic_types,
    )


def _nearest_indices(source_ns: np.ndarray, target_ns: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    right = np.searchsorted(source_ns, target_ns, side="left")
    right = np.clip(right, 0, len(source_ns) - 1)
    left = np.clip(right - 1, 0, len(source_ns) - 1)
    choose_left = np.abs(source_ns[left] - target_ns) <= np.abs(source_ns[right] - target_ns)
    indices = np.where(choose_left, left, right)
    skew_ns = np.abs(source_ns[indices] - target_ns)
    return indices.astype(np.int64), skew_ns


def _interpolate_tcp(
    pose_data: tuple[np.ndarray, np.ndarray, np.ndarray],
    target_ns: np.ndarray,
    spec: CameraSpec,
) -> tuple[np.ndarray, np.ndarray]:
    stamps, positions, quaternions = pose_data
    stamps_s = (stamps - stamps[0]).astype(np.float64) / 1e9
    target_s = (target_ns - stamps[0]).astype(np.float64) / 1e9
    interp_pos = np.column_stack(
        [np.interp(target_s, stamps_s, positions[:, axis]) for axis in range(3)]
    )
    rotations = Rotation.from_quat(quaternions)
    interp_rotation = Slerp(stamps_s, rotations)(target_s)
    tcp_rotation = interp_rotation * Rotation.from_quat(spec.tcp_rotation_xyzw)
    tcp_position = interp_pos + interp_rotation.apply(spec.tcp_translation_m)
    return tcp_position.astype(np.float32), tcp_rotation.as_rotvec().astype(np.float32)


def _tcp_sample_positions(
    pose_data: tuple[np.ndarray, np.ndarray, np.ndarray], spec: CameraSpec
) -> np.ndarray:
    _, positions, quaternions = pose_data
    rotations = Rotation.from_quat(quaternions)
    return positions + rotations.apply(spec.tcp_translation_m)


def _pose_quality_events(
    pose_data: tuple[np.ndarray, np.ndarray, np.ndarray],
    spec: CameraSpec,
    *,
    max_position_step_m: float,
    max_orientation_step_deg: float,
    max_pose_gap_ms: float,
) -> Dict[str, int]:
    stamps = pose_data[0]
    tcp_positions = _tcp_sample_positions(pose_data, spec)
    stamp_deltas = np.diff(stamps)
    position_steps = np.linalg.norm(np.diff(tcp_positions, axis=0), axis=1)
    rotations = Rotation.from_quat(pose_data[2])
    orientation_steps = (rotations[:-1].inv() * rotations[1:]).magnitude()
    gap_breaks = (stamp_deltas <= 0) | (stamp_deltas > max_pose_gap_ms * 1e6)
    jump_breaks = position_steps > max_position_step_m
    orientation_breaks = orientation_steps > math.radians(max_orientation_step_deg)
    return {
        "position_jumps": int(np.count_nonzero(jump_breaks)),
        "orientation_jumps": int(np.count_nonzero(orientation_breaks)),
        "tracking_gaps": int(np.count_nonzero(gap_breaks)),
    }


def _resampled_pose_events(
    positions: np.ndarray,
    rotvecs: np.ndarray,
    *,
    max_position_step_m: float,
    max_orientation_step_deg: float,
) -> Dict[str, int]:
    position_steps = np.linalg.norm(
        np.diff(positions.astype(np.float64), axis=0), axis=1
    )
    rotations = Rotation.from_rotvec(rotvecs.astype(np.float64))
    orientation_steps = (rotations[:-1].inv() * rotations[1:]).magnitude()
    return {
        "resampled_position_jumps": int(
            np.count_nonzero(position_steps > max_position_step_m)
        ),
        "resampled_orientation_jumps": int(
            np.count_nonzero(
                orientation_steps > math.radians(max_orientation_step_deg)
            )
        ),
    }


def build_episode_plans(
    bag_name: str,
    scan: StreamScan,
    specs: list[CameraSpec],
    *,
    fps: float,
    max_image_skew_ms: float,
    minimum_frames: int,
    max_position_step_m: float = DEFAULT_MAX_POSITION_STEP_M,
    max_orientation_step_deg: float = DEFAULT_MAX_ORIENTATION_STEP_DEG,
    max_pose_gap_ms: float = DEFAULT_MAX_POSE_GAP_MS,
) -> list[EpisodePlan]:
    starts = [values[0] for values in scan.image_stamps.values()]
    ends = [values[-1] for values in scan.image_stamps.values()]
    for stamps, _, _ in scan.poses.values():
        starts.append(stamps[0])
        ends.append(stamps[-1])
    start_ns = max(starts)
    end_ns = min(ends)
    step_ns = int(round(1e9 / fps))
    start_ns = int(math.ceil(start_ns / step_ns) * step_ns)
    timeline = np.arange(start_ns, end_ns + 1, step_ns, dtype=np.int64)
    if timeline.size < minimum_frames:
        raise ValueError("overlapping streams are shorter than the minimum episode length")

    image_indices = {}
    valid = np.ones(timeline.shape, dtype=bool)
    max_skew_ns = int(max_image_skew_ms * 1e6)
    observed_max_skew_ns = 0
    for spec in specs:
        indices, skew = _nearest_indices(scan.image_stamps[spec.name], timeline)
        image_indices[spec.name] = indices
        valid &= skew <= max_skew_ns
        observed_max_skew_ns = max(observed_max_skew_ns, int(skew.max()))

    if not np.all(valid):
        invalid_frames = int(np.count_nonzero(~valid))
        raise ValueError(
            f"episode rejected: {invalid_frames} synchronized frames exceed "
            f"the {max_image_skew_ms:g} ms image skew limit"
        )

    hand_specs = [spec for spec in specs if spec.role != "head"]
    pose_quality_events = {}
    for spec in hand_specs:
        events = _pose_quality_events(
            scan.poses[spec.name],
            spec,
            max_position_step_m=max_position_step_m,
            max_orientation_step_deg=max_orientation_step_deg,
            max_pose_gap_ms=max_pose_gap_ms,
        )
        pose_quality_events[spec.name] = events
    source_event_count = sum(
        count for events in pose_quality_events.values() for count in events.values()
    )
    if source_event_count:
        details = ", ".join(
            f"{name} {event}={count}"
            for name, events in pose_quality_events.items()
            for event, count in events.items()
            if count
        )
        raise ValueError(
            f"episode rejected by source pose continuity gate: {details}"
        )

    lowdim = {}
    for robot_index, spec in enumerate(hand_specs):
        position, rotvec = _interpolate_tcp(scan.poses[spec.name], timeline, spec)
        resampled_events = _resampled_pose_events(
            position,
            rotvec,
            max_position_step_m=max_position_step_m,
            max_orientation_step_deg=max_orientation_step_deg,
        )
        pose_quality_events[spec.name].update(resampled_events)
        if any(resampled_events.values()):
            details = ", ".join(
                f"{event}={count}"
                for event, count in resampled_events.items()
                if count
            )
            raise ValueError(
                f"episode rejected by 20 Hz pose continuity gate: "
                f"{spec.name} {details}"
            )
        opening_stamps, opening_values = scan.openings[spec.name]
        opening_base = opening_stamps[0]
        opening = np.interp(
            (timeline - opening_base).astype(np.float64),
            (opening_stamps - opening_base).astype(np.float64),
            opening_values,
        ).astype(np.float32)[:, None]
        pose = np.concatenate((position, rotvec), axis=1).astype(np.float32)
        lowdim[f"robot{robot_index}_eef_pos"] = position
        lowdim[f"robot{robot_index}_eef_rot_axis_angle"] = rotvec
        lowdim[f"robot{robot_index}_gripper_width"] = opening
        lowdim[f"robot{robot_index}_demo_start_pose"] = np.repeat(
            pose[:1], len(pose), axis=0
        )
        lowdim[f"robot{robot_index}_demo_end_pose"] = np.repeat(
            pose[-1:], len(pose), axis=0
        )
    return [
        EpisodePlan(
            bag_name=bag_name,
            timestamps_ns=timeline,
            image_indices=image_indices,
            lowdim=lowdim,
            detection_rates=scan.detection_rates,
            max_image_skew_ms=round(observed_max_skew_ns / 1e6, 3),
            pose_quality_events=pose_quality_events,
        )
    ]


def _prepare_rgb(image: np.ndarray, size: Optional[int]) -> np.ndarray:
    if image.ndim == 2:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
    else:
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    if size is None:
        return np.ascontiguousarray(image)
    interpolation = cv2.INTER_AREA if max(image.shape[:2]) > size else cv2.INTER_LINEAR
    return cv2.resize(image, (size, size), interpolation=interpolation)


def append_episode_images(
    writer: ZarrV2Writer,
    bag_path: Path,
    specs: list[CameraSpec],
    plan: EpisodePlan,
    *,
    size: Optional[int],
    output_shapes: Dict[str, tuple[int, int]],
    output_offset: int,
) -> None:
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_message

    topics = [spec.image_topic for spec in specs]
    reader, topic_types = _open_reader(bag_path, topics)
    message_classes = {
        topic: get_message(topic_types[topic]) for topic in topics
    }
    spec_by_topic = {spec.image_topic: (index, spec) for index, spec in enumerate(specs)}
    wanted = {
        spec.name: {int(source): target for target, source in enumerate(plan.image_indices[spec.name])}
        for spec in specs
    }
    source_index = {spec.name: 0 for spec in specs}
    written = {spec.name: 0 for spec in specs}
    while reader.has_next():
        topic, raw, _record_stamp_ns = reader.read_next()
        camera_index, spec = spec_by_topic[topic]
        current_index = source_index[spec.name]
        target_index = wanted[spec.name].get(current_index)
        source_index[spec.name] += 1
        if target_index is None:
            continue
        message = deserialize_message(raw, message_classes[topic])
        image = decode_image(message, topic_types[topic])
        if image is None:
            raise ValueError(f"failed to decode {topic} frame {current_index}")
        actual_shape = (int(image.shape[0]), int(image.shape[1]))
        expected_source_shape = output_shapes[spec.name]
        if size is None and actual_shape != expected_source_shape:
            raise ValueError(
                f"{spec.name} image resolution changed from "
                f"{expected_source_shape[1]}x{expected_source_shape[0]} to "
                f"{actual_shape[1]}x{actual_shape[0]}"
            )
        writer.write_frame(
            f"data/camera{camera_index}_rgb",
            output_offset + target_index,
            _prepare_rgb(image, size),
        )
        written[spec.name] += 1
    missing = {
        name: len(plan.timestamps_ns) - count
        for name, count in written.items()
        if count != len(plan.timestamps_ns)
    }
    if missing:
        raise ValueError(f"failed to write selected image frames: {missing}")


def _zip_store(directory: Path, output_path: Path) -> None:
    pending = output_path.with_suffix(output_path.suffix + ".pending")
    pending.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(pending, "w", compression=zipfile.ZIP_STORED) as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(directory).as_posix())
    pending.replace(output_path)


def _write_training_config(
    output_path: Path,
    dataset_name: str,
    fps: float,
    camera_shapes: list[tuple[int, int]],
    robot_count: int,
) -> Path:
    config_path = output_path.with_name(f"{dataset_name}.umi.yaml")
    observations = []
    for camera_index, (height, width) in enumerate(camera_shapes):
        observations.extend(
            [
                f"    camera{camera_index}_rgb:",
                f"      shape: [3, {height}, {width}]",
                "      horizon: 2",
                "      latency_steps: 0",
                "      down_sample_steps: 1",
                "      type: rgb",
                "      ignore_by_policy: false",
            ]
        )
    for robot_index in range(robot_count):
        observations.extend(
            [
                f"    robot{robot_index}_eef_pos:",
                "      shape: [3]",
                "      horizon: 2",
                "      latency_steps: 0",
                "      down_sample_steps: 1",
                "      type: low_dim",
                "      ignore_by_policy: false",
                f"    robot{robot_index}_eef_rot_axis_angle:",
                "      raw_shape: [3]",
                "      shape: [6]",
                "      horizon: 2",
                "      latency_steps: 0",
                "      down_sample_steps: 1",
                "      type: low_dim",
                "      rotation_rep: rotation_6d",
                "      ignore_by_policy: false",
                f"    robot{robot_index}_gripper_width:",
                "      shape: [1]",
                "      horizon: 2",
                "      latency_steps: 0",
                "      down_sample_steps: 1",
                "      type: low_dim",
                "      ignore_by_policy: false",
            ]
        )
    text = "\n".join(
        [
            "# Copy this file into diffusion_policy/config/task/ and update dataset_path.",
            f"name: {dataset_name}",
            f"dataset_frequeny: {float(fps):g}",
            "shape_meta: &shape_meta",
            "  obs:",
            *observations,
            "  action:",
            f"    shape: [{robot_count * 10}]",
            "    horizon: 16",
            "    latency_steps: 0",
            "    down_sample_steps: 1",
            "    rotation_rep: rotation_6d",
            f"dataset_path: /path/to/{dataset_name}.zarr.zip",
            "pose_repr: &pose_repr",
            "  obs_pose_repr: relative",
            "  action_pose_repr: relative",
            "dataset:",
            "  _target_: diffusion_policy.dataset.umi_dataset.UmiDataset",
            "  shape_meta: *shape_meta",
            "  dataset_path: ${task.dataset_path}",
            "  cache_dir: null",
            "  pose_repr: *pose_repr",
            "  action_padding: false",
            "  temporally_independent_normalization: false",
            "  repeat_frame_prob: 0.0",
            "  max_duration: null",
            "  seed: 42",
            "  val_ratio: 0.05",
            "",
        ]
    )
    config_path.write_text(text, encoding="utf-8")
    return config_path


def export_umi_dataset(
    bag_paths: list[Path],
    output_path: Path,
    *,
    camera_config: Path,
    calibration_path: Path,
    fps: float = 20.0,
    image_size: Optional[int] = None,
    max_image_skew_ms: float = 40.0,
    minimum_frames: int = 24,
    max_position_step_m: float = DEFAULT_MAX_POSITION_STEP_M,
    max_orientation_step_deg: float = DEFAULT_MAX_ORIENTATION_STEP_DEG,
    max_pose_gap_ms: float = DEFAULT_MAX_POSE_GAP_MS,
    camera_names: Optional[list[str]] = None,
) -> dict[str, object]:
    specs = load_camera_specs(camera_config, camera_names)
    robot_count = sum(spec.role != "head" for spec in specs)
    bag_paths = [path.resolve() for path in bag_paths]
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    planned_episodes: list[tuple[Path, EpisodePlan]] = []
    source_shapes: Optional[Dict[str, tuple[int, int]]] = None
    for bag_index, bag_path in enumerate(bag_paths):
        bag_path = bag_path.resolve()
        print(
            f"UMI_PROGRESS {bag_index + 1} {len(bag_paths)} scan {bag_path.name} 0",
            flush=True,
        )
        scan = scan_bag(bag_path, specs, calibration_path)
        if source_shapes is None:
            source_shapes = dict(scan.image_shapes)
        elif scan.image_shapes != source_shapes:
            details = ", ".join(
                f"{name}: {source_shapes.get(name)} -> {scan.image_shapes.get(name)}"
                for name in sorted(set(source_shapes) | set(scan.image_shapes))
                if source_shapes.get(name) != scan.image_shapes.get(name)
            )
            raise ValueError(f"camera resolutions differ between rosbags: {details}")
        plans = build_episode_plans(
            bag_path.name,
            scan,
            specs,
            fps=fps,
            max_image_skew_ms=max_image_skew_ms,
            minimum_frames=minimum_frames,
            max_position_step_m=max_position_step_m,
            max_orientation_step_deg=max_orientation_step_deg,
            max_pose_gap_ms=max_pose_gap_ms,
        )
        planned_episodes.extend((bag_path, plan) for plan in plans)
    if not planned_episodes:
        raise ValueError("no valid episodes were generated")
    assert source_shapes is not None
    output_shapes = {
        spec.name: (
            (int(image_size), int(image_size))
            if image_size is not None
            else source_shapes[spec.name]
        )
        for spec in specs
    }

    total_frames = sum(len(plan.timestamps_ns) for _, plan in planned_episodes)
    episode_summaries = []
    episode_ends = []
    with tempfile.TemporaryDirectory(
        prefix="umi_export_", dir=str(output_path.parent)
    ) as temporary:
        store_path = Path(temporary) / "dataset.zarr"
        writer = ZarrV2Writer(store_path)
        writer.create_group("data")
        writer.create_group("meta")
        for camera_index, spec in enumerate(specs):
            height, width = output_shapes[spec.name]
            writer.create_array(
                f"data/camera{camera_index}_rgb",
                (total_frames, height, width, 3),
                (1, height, width, 3),
                np.uint8,
                compression_level=1,
            )

        output_offset = 0
        lowdim_parts: Dict[str, list[np.ndarray]] = {}
        timestamp_parts = []
        bag_indices = {path: index for index, path in enumerate(bag_paths)}
        segment_indices: Dict[str, int] = {}
        for bag_path, plan in planned_episodes:
            bag_index = bag_indices[bag_path]
            print(
                f"UMI_PROGRESS {bag_index + 1} {len(bag_paths)} images {bag_path.name} {output_offset}",
                flush=True,
            )
            append_episode_images(
                writer,
                bag_path,
                specs,
                plan,
                size=image_size,
                output_shapes=source_shapes,
                output_offset=output_offset,
            )
            for name, values in plan.lowdim.items():
                lowdim_parts.setdefault(name, []).append(values)
            timestamp_parts.append((plan.timestamps_ns.astype(np.float64) / 1e9)[:, None])
            output_offset += len(plan.timestamps_ns)
            episode_ends.append(output_offset)
            segment_index = segment_indices.get(bag_path.name, 0)
            segment_indices[bag_path.name] = segment_index + 1
            episode_summaries.append(
                {
                    "bag_name": bag_path.name,
                    "segment_index": segment_index,
                    "frames": len(plan.timestamps_ns),
                    "duration_s": round(len(plan.timestamps_ns) / fps, 3),
                    "gripper_detection_rates": plan.detection_rates,
                    "max_image_skew_ms": plan.max_image_skew_ms,
                    "pose_quality_events": plan.pose_quality_events,
                }
            )
        for name, parts in lowdim_parts.items():
            writer.write_array(f"data/{name}", np.concatenate(parts, axis=0))
        writer.write_array("data/timestamp", np.concatenate(timestamp_parts, axis=0))
        writer.write_array(
            "meta/episode_ends", np.asarray(episode_ends, dtype=np.int64)
        )
        writer.set_root_attrs(
            {
                "format": "umi_replay_buffer",
                "schema_version": SCHEMA_VERSION,
                "fps": float(fps),
                "image_mode": "original" if image_size is None else "resized_square",
                "camera_image_sizes": {
                    spec.name: [output_shapes[spec.name][1], output_shapes[spec.name][0]]
                    for spec in specs
                },
                "camera_order": [spec.name for spec in specs],
                "robot_order": [spec.name for spec in specs if spec.role != "head"],
                "pose_topics": {
                    spec.name: spec.pose_topic for spec in specs if spec.pose_topic
                },
                "gripper_width_semantics": "physical_jaw_width_m",
                "pose_quality_gate": {
                    "max_position_step_m": float(max_position_step_m),
                    "max_orientation_step_deg": float(max_orientation_step_deg),
                    "max_pose_gap_ms": float(max_pose_gap_ms),
                    "behavior": "reject_rosbag_episode_on_discontinuity",
                },
                "source_bags": [path.name for path in bag_paths],
            }
        )
        print(
            f"UMI_PROGRESS {len(bag_paths)} {len(bag_paths)} package dataset {total_frames}",
            flush=True,
        )
        _zip_store(store_path, output_path)

    elapsed = time.perf_counter() - started
    summary = {
        "output_path": str(output_path),
        "episode_count": len(episode_ends),
        "total_frames": total_frames,
        "duration_s": round(total_frames / fps, 3),
        "processing_seconds": round(elapsed, 3),
        "fps": float(fps),
        "image_mode": "original" if image_size is None else "resized_square",
        "camera_image_sizes": {
            spec.name: [output_shapes[spec.name][1], output_shapes[spec.name][0]]
            for spec in specs
        },
        "camera_order": [spec.name for spec in specs],
        "robot_order": [spec.name for spec in specs if spec.role != "head"],
        "pose_topics": {
            spec.name: spec.pose_topic for spec in specs if spec.pose_topic
        },
        "gripper_width_semantics": "physical_jaw_width_m",
        "pose_quality_gate": {
            "max_position_step_m": float(max_position_step_m),
            "max_orientation_step_deg": float(max_orientation_step_deg),
            "max_pose_gap_ms": float(max_pose_gap_ms),
        },
        "episodes": episode_summaries,
        "size_bytes": output_path.stat().st_size,
    }
    training_config = _write_training_config(
        output_path,
        output_path.name.removesuffix(".zarr.zip"),
        fps,
        [output_shapes[spec.name] for spec in specs],
        robot_count,
    )
    summary["training_config_path"] = str(training_config)
    manifest_path = output_path.with_name(
        f"{output_path.name.removesuffix('.zarr.zip')}.manifest.json"
    )
    manifest_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(f"UMI_DONE {total_frames} {len(episode_ends)} {output_path}", flush=True)
    return summary


def build_parser() -> argparse.ArgumentParser:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bags", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--camera-config", type=Path, default=project_root / "config" / "cameras.json"
    )
    parser.add_argument(
        "--calibration",
        type=Path,
        default=project_root / "config" / "gripper_calibration.json",
    )
    parser.add_argument("--fps", type=float, default=20.0)
    parser.add_argument(
        "--image-size",
        default="original",
        help="original, or a positive square output size such as 224",
    )
    parser.add_argument("--max-image-skew-ms", type=float, default=40.0)
    parser.add_argument("--minimum-frames", type=int, default=24)
    parser.add_argument(
        "--max-position-step-m",
        type=float,
        default=DEFAULT_MAX_POSITION_STEP_M,
        help="reject an episode when consecutive TCP samples exceed this distance",
    )
    parser.add_argument(
        "--max-orientation-step-deg",
        type=float,
        default=DEFAULT_MAX_ORIENTATION_STEP_DEG,
        help="reject an episode when consecutive TCP orientations exceed this angle",
    )
    parser.add_argument(
        "--max-pose-gap-ms",
        type=float,
        default=DEFAULT_MAX_POSE_GAP_MS,
        help="reject an episode when VIO tracking gaps exceed this duration",
    )
    parser.add_argument(
        "--camera",
        dest="camera_names",
        action="append",
        help="camera name to include; repeat for multiple cameras",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        image_size = None if args.image_size == "original" else int(args.image_size)
    except ValueError:
        image_size = -1
    if (
        args.fps <= 0
        or (image_size is not None and image_size <= 0)
        or args.minimum_frames < 2
        or args.max_position_step_m <= 0
        or args.max_orientation_step_deg <= 0
        or args.max_pose_gap_ms <= 0
    ):
        print("ERROR: invalid export parameters", file=sys.stderr)
        return 2
    try:
        export_umi_dataset(
            args.bags,
            args.output,
            camera_config=args.camera_config,
            calibration_path=args.calibration,
            fps=args.fps,
            image_size=image_size,
            max_image_skew_ms=args.max_image_skew_ms,
            minimum_frames=args.minimum_frames,
            max_position_step_m=args.max_position_step_m,
            max_orientation_step_deg=args.max_orientation_step_deg,
            max_pose_gap_ms=args.max_pose_gap_ms,
            camera_names=args.camera_names,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
