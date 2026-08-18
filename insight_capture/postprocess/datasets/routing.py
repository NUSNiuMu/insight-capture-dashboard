"""Choose the LeRobot export pipeline from gripper markers in a rosbag."""

from __future__ import annotations

from pathlib import Path
from typing import Dict

from insight_capture.postprocess.gripper.extraction import decode_image
from insight_capture.postprocess.gripper.tracking import GripperMarkerDetector


DEFAULT_SAMPLE_STRIDE = 10
DEFAULT_REQUIRED_DUAL_MARKER_HITS = 2


def select_lerobot_route(
    camera_results: Dict[str, Dict[str, int]],
    *,
    required_hits: int = DEFAULT_REQUIRED_DUAL_MARKER_HITS,
) -> str:
    """Use UMI when any wrist view repeatedly contains both gripper markers."""

    return (
        "umi_gripper"
        if any(
            int(result.get("dual_marker_hits", 0)) >= required_hits
            for result in camera_results.values()
        )
        else "ego_hand"
    )


def inspect_gripper_markers(
    bag_path: Path,
    image_topics: Dict[str, str],
    *,
    sample_stride: int = DEFAULT_SAMPLE_STRIDE,
    required_hits: int = DEFAULT_REQUIRED_DUAL_MARKER_HITS,
) -> Dict[str, object]:
    """Sample wrist images and return the deterministic LeRobot route."""

    if sample_stride < 1 or required_hits < 1:
        raise ValueError("marker sampling parameters must be positive")
    try:
        import rosbag2_py
        from rclpy.serialization import deserialize_message
        from rosidl_runtime_py.utilities import get_message
    except ImportError as exc:
        raise RuntimeError("ROS 2 rosbag Python dependencies are unavailable") from exc

    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(Path(bag_path).resolve()), storage_id=""),
        rosbag2_py.ConverterOptions(
            input_serialization_format="cdr", output_serialization_format="cdr"
        ),
    )
    topic_types = {
        item.name: item.type for item in reader.get_all_topics_and_types()
    }
    missing = sorted(topic for topic in image_topics.values() if topic not in topic_types)
    if missing:
        raise ValueError(f"missing marker-inspection topics: {', '.join(missing)}")
    reader.set_filter(rosbag2_py.StorageFilter(topics=list(image_topics.values())))
    camera_by_topic = {topic: camera for camera, topic in image_topics.items()}
    message_classes = {
        topic: get_message(topic_types[topic]) for topic in image_topics.values()
    }
    detectors = {camera: GripperMarkerDetector() for camera in image_topics}
    results: Dict[str, Dict[str, int]] = {
        camera: {"frames_seen": 0, "frames_sampled": 0, "dual_marker_hits": 0}
        for camera in image_topics
    }
    while reader.has_next():
        topic, raw, _stamp = reader.read_next()
        camera = camera_by_topic[topic]
        result = results[camera]
        frame_index = result["frames_seen"]
        result["frames_seen"] += 1
        if frame_index % sample_stride:
            continue
        message = deserialize_message(raw, message_classes[topic])
        image = decode_image(message, topic_types[topic])
        result["frames_sampled"] += 1
        detection = detectors[camera].detect(image) if image is not None else None
        if detection is not None and detection.distance_px is not None:
            result["dual_marker_hits"] += 1
        if all(
            item["dual_marker_hits"] >= required_hits for item in results.values()
        ):
            break

    return {
        "route": select_lerobot_route(results, required_hits=required_hits),
        "sample_stride": sample_stride,
        "required_dual_marker_hits": required_hits,
        "cameras": results,
    }


def build_ego_spec(
    bag_path: Path,
    camera_config: Path,
    *,
    dataset_id: str,
    task: str,
) -> Dict[str, object]:
    """Create a full-head-timeline, single-action spec for automatic hand export."""

    from insight_capture.postprocess.datasets.ego_lerobot.rosbag_io import load_camera_streams, load_topic_stamps

    streams = load_camera_streams(camera_config)
    stamps = load_topic_stamps(Path(bag_path), streams["head"].image_topic)
    duration_s = float((int(stamps[-1]) - int(stamps[0])) / 1e9)
    return {
        "schema_version": 1,
        "dataset_id": dataset_id,
        "task": task,
        "fps": 30.0,
        "crop": {"start_s": 0.0, "end_s": duration_s},
        "segments": [
            {
                "segment_index": 0,
                "subtask": "demonstration",
                "atomic_action": "perform_task",
                "task": task,
                "start_frame": 0,
                "end_frame": int(len(stamps) - 1),
            }
        ],
    }
