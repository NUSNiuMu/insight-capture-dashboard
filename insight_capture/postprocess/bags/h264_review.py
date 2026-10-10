"""Decode complete H.264 dependencies before selecting synchronized review frames."""

import time

from insight_capture.media.h264 import H264Gate
from insight_capture.media.h264_decode import H264Decoder


def first_decodable_stamps(bag_path, cameras, check_cancelled):
    names = {c["topic"]: c["name"] for c in cameras if c.get("type") == "h264"}
    gates = {topic: H264Gate() for topic in names}
    starts = {}
    if not gates:
        return starts
    from rclpy.serialization import deserialize_message
    from sensor_msgs.msg import CompressedImage
    from .synchronization import _sqlite_topic_messages
    for topic, raw, stamp in _sqlite_topic_messages(bag_path, names):
        check_cancelled()
        if names[topic] in starts:
            continue
        message = deserialize_message(raw, CompressedImage)
        if message.format.lower() != "h264":
            raise ValueError(f"{topic}: expected H.264 access units")
        if gates[topic].accept(bytes(message.data)) is not None:
            starts[names[topic]] = int(stamp)
        if len(starts) == len(names):
            break
    missing = set(names.values()) - set(starts)
    if missing:
        raise ValueError(f"H.264 streams have no decodable IDR: {sorted(missing)}")
    return starts


def encode_review(manager, bag_path, cameras, selections, output, progress):
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_message
    from .synchronization import (
        _sqlite_topic_messages, _sqlite_topic_types, _decode_review_image,
        _video_writer, _compose_review_frame, REVIEW_HEIGHT, REVIEW_WIDTH, PLAYBACK_FPS,
    )
    by_topic = {str(c["topic"]): c for c in cameras}
    types = _sqlite_topic_types(bag_path)
    classes = {topic: get_message(types[topic]) for topic in by_topic}
    indices = {c["name"]: 0 for c in cameras}
    wanted = {}
    for name, selection in selections.items():
        wanted[name] = {}
        for target, source in enumerate(selection):
            wanted[name].setdefault(int(source), []).append(target)
    count = len(next(iter(selections.values())))
    pending = {}
    dimensions = {}
    decoders = {}
    source_stamps = {}
    next_target = 0
    writer = None
    shape = (REVIEW_HEIGHT, REVIEW_WIDTH)

    def consume(name, source, frame):
        nonlocal next_target
        dimensions.setdefault(name, frame.shape[:2])
        for target in wanted[name].get(source, []):
            pending.setdefault(target, {})[name] = frame
        while len(pending.get(next_target, {})) == len(cameras):
            manager._check_cancelled()
            frames = pending.pop(next_target)
            duplicates = [next_target > 0 and selections[c["name"]][next_target] == selections[c["name"]][next_target-1]
                          for c in cameras]
            writer.write(_compose_review_frame(
                [frames[c["name"]] for c in cameras], cameras, duplicates, next_target,
                bgrx=bool(getattr(writer, "uses_bgrx", False)),
            ))
            next_target += 1
            if next_target % 10 == 0:
                progress(next_target)

    def drain(name, wait=0):
        decoder = decoders[name]
        result = decoder.pull(wait)
        while result is not None:
            pts, frame = result
            consume(name, int(round(pts/1_000_000_000)), frame)
            result = decoder.pull()

    try:
        writer = _video_writer(output, shape, PLAYBACK_FPS)
        with manager._lock:
            manager._writer = writer
        for topic, raw, _stamp in _sqlite_topic_messages(bag_path, by_topic):
            manager._check_cancelled()
            camera = by_topic[topic]
            name = camera["name"]
            index = indices[name]
            indices[name] += 1
            if camera.get("type") == "h264":
                if name not in decoders:
                    decoders[name] = H264Decoder()
                message = deserialize_message(raw, classes[topic])
                if message.format.lower() != "h264":
                    raise ValueError(f"{topic}: expected H.264 access units")
                stamp = message.header.stamp.sec * 1_000_000_000 + message.header.stamp.nanosec
                previous = source_stamps.get(name)
                if previous is not None and not 0 < stamp - previous <= 50_000_000:
                    gaps = manager._review_codec_discontinuities
                    gaps[name] = gaps.get(name, 0) + 1
                source_stamps[name] = stamp
                decoders[name].push(bytes(message.data), index * 1_000_000_000)
                drain(name, 50_000_000)
            elif index in wanted[name]:
                message = deserialize_message(raw, classes[topic])
                consume(name, index, _decode_review_image(message, types[topic], False))
        for name, decoder in decoders.items():
            decoder.finish()
            deadline = time.monotonic() + 10
            while not decoder.sink.get_property("eos") and time.monotonic() < deadline:
                manager._check_cancelled()
                drain(name, 100_000_000)
        if next_target != count:
            raise RuntimeError(f"H.264 review decoded {next_target}/{count} synchronized frames")
        writer.close()
        return shape, writer.frames, writer.encoder_name, dimensions
    finally:
        if writer is not None:
            writer.abort()
        for decoder in decoders.values():
            decoder.close()
        with manager._lock:
            manager._writer = None
