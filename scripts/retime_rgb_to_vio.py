#!/usr/bin/env python3

import argparse
import bisect
from pathlib import Path

import rosbag2_py
from rclpy.serialization import deserialize_message, serialize_message
from rosidl_runtime_py.utilities import get_message


def stamp_to_ns(stamp) -> int:
    return int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)


def ns_to_stamp_fields(ns: int) -> tuple[int, int]:
    sec = ns // 1_000_000_000
    nanosec = ns % 1_000_000_000
    return sec, nanosec


def load_vio_stamps(reader: rosbag2_py.SequentialReader, vio_topic: str) -> list[int]:
    vio_cls = get_message("geometry_msgs/msg/PoseStamped")
    stamps: list[int] = []
    while reader.has_next():
        topic, data, _ = reader.read_next()
        if topic != vio_topic:
            continue
        msg = deserialize_message(data, vio_cls)
        stamps.append(stamp_to_ns(msg.header.stamp))
    if not stamps:
        raise RuntimeError(f"no messages found on VIO topic: {vio_topic}")
    return stamps


def build_nearest_stamp_map(source_stamps: list[int], vio_stamps: list[int]) -> list[int]:
    mapped: list[int] = []
    last_idx = -1
    for stamp in source_stamps:
        idx = bisect.bisect_left(vio_stamps, stamp)
        candidates = []
        if idx < len(vio_stamps):
            candidates.append(idx)
        if idx > 0:
            candidates.append(idx - 1)
        best_idx = min(candidates, key=lambda i: abs(vio_stamps[i] - stamp))
        if best_idx <= last_idx:
            best_idx = min(last_idx + 1, len(vio_stamps) - 1)
        if best_idx <= last_idx:
            raise RuntimeError("failed to build a strictly increasing VIO stamp mapping")
        mapped.append(vio_stamps[best_idx])
        last_idx = best_idx
    return mapped


def main() -> None:
    parser = argparse.ArgumentParser(description="Overwrite RGB header timestamps with nearest VIO header timestamps.")
    parser.add_argument("input_bag", help="Input rosbag directory")
    parser.add_argument("output_bag", help="Output rosbag directory")
    parser.add_argument("--storage-id", default="mcap")
    parser.add_argument("--rgb-topic", default="/insight_relay/insight9_a/camera/color/image_rect_raw/compressed")
    parser.add_argument("--camera-info-topic", default="/insight_relay/insight9_a/camera/color/camera_info")
    parser.add_argument("--vio-topic", default="/insight_relay/insight9_a/camera/vio_100hz")
    args = parser.parse_args()

    input_bag = Path(args.input_bag).resolve()
    output_bag = Path(args.output_bag).resolve()
    if not input_bag.exists():
        raise FileNotFoundError(f"input bag does not exist: {input_bag}")
    if output_bag.exists():
        raise FileExistsError(f"output bag already exists: {output_bag}")

    read_options = rosbag2_py.StorageOptions(uri=str(input_bag), storage_id=args.storage_id)
    convert_options = rosbag2_py.ConverterOptions("", "")

    first_reader = rosbag2_py.SequentialReader()
    first_reader.open(read_options, convert_options)
    vio_stamps = load_vio_stamps(first_reader, args.vio_topic)

    second_reader = rosbag2_py.SequentialReader()
    second_reader.open(read_options, convert_options)
    topics_and_types = second_reader.get_all_topics_and_types()
    topic_types = {item.name: item.type for item in topics_and_types}
    if args.rgb_topic not in topic_types:
        raise RuntimeError(f"RGB topic not found in bag: {args.rgb_topic}")
    if args.camera_info_topic not in topic_types:
        raise RuntimeError(f"camera_info topic not found in bag: {args.camera_info_topic}")

    rgb_cls = get_message(topic_types[args.rgb_topic])
    camera_info_cls = get_message(topic_types[args.camera_info_topic])

    rgb_original_stamps: list[int] = []
    camera_info_original_stamps: list[int] = []
    while second_reader.has_next():
        topic, data, _ = second_reader.read_next()
        if topic == args.rgb_topic:
            msg = deserialize_message(data, rgb_cls)
            rgb_original_stamps.append(stamp_to_ns(msg.header.stamp))
        elif topic == args.camera_info_topic:
            msg = deserialize_message(data, camera_info_cls)
            camera_info_original_stamps.append(stamp_to_ns(msg.header.stamp))

    if len(rgb_original_stamps) != len(camera_info_original_stamps):
        raise RuntimeError(
            f"RGB and camera_info counts differ: {len(rgb_original_stamps)} vs {len(camera_info_original_stamps)}"
        )

    mapped_stamps = build_nearest_stamp_map(rgb_original_stamps, vio_stamps)

    writer = rosbag2_py.SequentialWriter()
    write_options = rosbag2_py.StorageOptions(uri=str(output_bag), storage_id=args.storage_id)
    writer.open(write_options, convert_options)
    for item in topics_and_types:
        writer.create_topic(
            rosbag2_py.TopicMetadata(
                name=item.name,
                type=item.type,
                serialization_format=item.serialization_format,
                offered_qos_profiles=item.offered_qos_profiles,
            )
        )

    third_reader = rosbag2_py.SequentialReader()
    third_reader.open(read_options, convert_options)
    message_classes = {name: get_message(type_name) for name, type_name in topic_types.items()}
    rgb_idx = 0
    camera_info_idx = 0

    while third_reader.has_next():
        topic, data, bag_ts = third_reader.read_next()
        if topic == args.rgb_topic:
            msg = deserialize_message(data, message_classes[topic])
            sec, nanosec = ns_to_stamp_fields(mapped_stamps[rgb_idx])
            msg.header.stamp.sec = sec
            msg.header.stamp.nanosec = nanosec
            data = serialize_message(msg)
            rgb_idx += 1
        elif topic == args.camera_info_topic:
            msg = deserialize_message(data, message_classes[topic])
            sec, nanosec = ns_to_stamp_fields(mapped_stamps[camera_info_idx])
            msg.header.stamp.sec = sec
            msg.header.stamp.nanosec = nanosec
            data = serialize_message(msg)
            camera_info_idx += 1
        writer.write(topic, data, bag_ts)

    writer.close()

    print(
        f"retimed {rgb_idx} RGB messages and {camera_info_idx} camera_info messages "
        f"from {input_bag.name} -> {output_bag.name}"
    )


if __name__ == "__main__":
    main()
