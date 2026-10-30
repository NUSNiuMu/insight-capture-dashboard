"""Join verified local and Ego bags on original capture timestamps."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import struct
import time

import numpy as np

from insight_capture.media.h264 import nal_units, H264ReferenceSequence
from insight_capture.quality.topic_rates import nominal_for


def write_json(path: Path, value: dict) -> None:
    """Durably replace coordinator intent before sending device commands."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024**2), b""):
            digest.update(chunk)
    return digest.hexdigest()


def open_reader(path: Path):
    import rosbag2_py
    reader = rosbag2_py.SequentialReader()
    reader.open(rosbag2_py.StorageOptions(uri=str(path), storage_id="mcap"), rosbag2_py.ConverterOptions("", ""))
    return reader


def header_ns(raw: bytes) -> int:
    sec, nanosec = struct.unpack_from(("<" if raw[1] & 1 else ">") + "iI", raw, 4)
    if not 0 <= nanosec < 10**9:
        raise ValueError("Invalid CDR header timestamp")
    return sec * 10**9 + nanosec


def header_quality(stats: dict, image_topics: list, maximum_gap_pct: float = 0.1) -> dict:
    """Report capture-header gaps separately from encoded reference continuity."""
    topics, issues = {}, []
    for topic in image_topics:
        stat = stats[topic]
        estimated = int(stat["missing"])
        percentage = estimated * 100 / max(1, int(stat["count"]) + estimated)
        topics[topic] = {"header_gap_pct": percentage, "estimated_missing": estimated,
                         "reference_gaps": stat.get("reference_gaps"), "frames": stat["count"]}
        if percentage > maximum_gap_pct or stat.get("reference_gaps", 0):
            issues.append(f"{topic}: header gap estimate {percentage:.3f}%, reference gaps {stat.get('reference_gaps', 'n/a')}")
    return {"ok": not issues, "max_header_gap_pct": maximum_gap_pct, "topics": topics, "issues": issues}


def merge_session(session: dict, config: dict, ego: Path, wrists: Path, output: Path) -> dict:
    """Keep raw bags intact and write one MCAP using unmodified CDR payloads."""
    import rosbag2_py
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_message

    image_topics = config["image_topics"]
    descriptors, header_topics, stats = {}, set(), {}
    reference_trackers = {}
    lower = int((session["created_at_epoch_s"] - 60) * 1e9)
    upper = int((session.get("stopped_at_epoch_s", time.time()) + 60) * 1e9)
    for part in (wrists, ego):
        reader = open_reader(part)
        for descriptor in reader.get_all_topics_and_types():
            if descriptor.name in descriptors and descriptors[descriptor.name].type != descriptor.type:
                raise ValueError("Topic type differs across participants: " + descriptor.name)
            descriptors[descriptor.name] = descriptor
            if get_message(descriptor.type)().get_fields_and_field_types().get("header") == "std_msgs/Header":
                header_topics.add(descriptor.name)
        while reader.has_next():
            topic, raw, receipt = reader.read_next()
            if topic not in header_topics:
                continue
            stamp = header_ns(raw)
            if not lower <= stamp <= upper:
                raise RuntimeError(f"{topic} 原始时间戳不在本次 UTC 录制区间，拒绝按到达时间替代")
            stat = stats.setdefault(topic, {"count": 0, "first_ns": stamp, "last_ns": stamp, "rollbacks": 0, "missing": 0})
            if stat["count"] and stamp < stat["last_ns"]:
                stat["rollbacks"] += 1
            rate = 30.0 if topic in image_topics else nominal_for(topic)
            if rate and stat["count"] and stamp - stat["last_ns"] > 1.5e9 / rate:
                stat["missing"] += max(0, round((stamp - stat["last_ns"]) * rate / 1e9) - 1)
            stat["count"] += 1
            stat["last_ns"] = stamp
            if topic in image_topics:
                stat.setdefault("stamps", []).append(stamp)
                if topic.endswith("/h264"):
                    message = deserialize_message(raw, get_message(descriptors[topic].type))
                    data = bytes(message.data)
                    sequence = reference_trackers.setdefault(topic, H264ReferenceSequence()).observe(data)
                    stat["reference_gaps"] = stat.get("reference_gaps", 0) + int(sequence is False)
                    kinds = {kind for kind, _ in nal_units(data)}
                    if {5, 7, 8}.issubset(kinds):
                        stat.setdefault("first_idr_ns", stamp)
        del reader
    missing = [topic for topic in image_topics if topic not in stats or stats[topic]["count"] < 2]
    if missing:
        raise RuntimeError("合并缺少必要图像：" + ", ".join(missing))
    if any(stat["rollbacks"] for stat in stats.values()):
        raise RuntimeError("检测到采集时间戳回退，原始文件保留，禁止自动合并")
    starts = {}
    for topic in image_topics:
        stat = stats[topic]
        starts[topic] = stat.get("first_idr_ns") if topic.endswith("/h264") else stat["first_ns"]
        if starts[topic] is None:
            raise RuntimeError("H.264 缺少同时包含 SPS/PPS 的 IDR：" + topic)
    start, end = max(starts.values()), min(stats[t]["last_ns"] for t in image_topics)
    if end - start < 1_000_000_000:
        raise RuntimeError("图像流共同可解码区间不足一秒")
    reference = np.asarray(stats[image_topics[0]]["stamps"], dtype=np.int64)
    reference = reference[(reference >= start) & (reference <= end)]
    offsets = {}
    for topic in image_topics:
        stamps = np.asarray(stats[topic].pop("stamps"), dtype=np.int64)
        positions = np.searchsorted(stamps, reference)
        left = stamps[np.maximum(0, positions - 1)]
        right = stamps[np.minimum(len(stamps) - 1, positions)]
        deltas = np.minimum(np.abs(reference - left), np.abs(reference - right)) / 1e6
        offsets[topic] = {"nearest_header_max_ms": float(deltas.max()), "nearest_header_p95_ms": float(np.percentile(deltas, 95))}
    if output.exists():
        raise RuntimeError("合并输出已存在，拒绝覆盖：" + str(output))
    temporary = output.with_name("." + output.name + ".merging")
    if temporary.exists():
        import shutil
        shutil.rmtree(temporary)
    writer = rosbag2_py.SequentialWriter()
    writer.open(rosbag2_py.StorageOptions(uri=str(temporary), storage_id="mcap"), rosbag2_py.ConverterOptions("", ""))
    for descriptor in descriptors.values():
        writer.create_topic(descriptor)
    counts = {}
    for part in (wrists, ego):
        reader = open_reader(part)
        while reader.has_next():
            topic, raw, receipt = reader.read_next()
            stamp = header_ns(raw) if topic in header_topics else receipt
            keep = topic not in header_topics or "camera_info" in topic
            if topic in image_topics:
                keep = starts[topic] <= stamp <= end
            elif topic in header_topics and not keep:
                keep = start - 200_000_000 <= stamp <= end + 200_000_000
            if keep:
                writer.write(topic, raw, stamp)
                counts[topic] = counts.get(topic, 0) + 1
        del reader
    del writer
    report = {
        "session_id": session["session_id"], "task_id": session.get("task_id"),
        "timestamp_policy": "original header UTC for header-bearing messages; raw CDR unchanged; receipt timestamps retained in raw participant bags",
        "common_start_ns": start, "common_end_ns": end, "duration_sec": (end - start) / 1e9,
        "episode_t0_ns": start, "video_decode_start_ns": starts,
        "decoder_preroll_policy": "each H264 stream starts at its own SPS/PPS/IDR at or before the common interval",
        "frame_alignment": offsets, "source_topic_audit": stats, "written_counts": counts,
        "clock_ok": not session.get("clock_violations"),
        "clock_violations": session.get("clock_violations", []),
        "video_header_quality": header_quality(stats, image_topics, float(config.get("max_video_header_gap_pct", 0.1))),
        "exposure_sync": "NTP clock agreement and header matching; hardware exposure synchronization not claimed",
    }
    # Verify the resulting file is readable and counts match before publishing it.
    reader = open_reader(temporary)
    observed = {}
    while reader.has_next():
        topic, _, _ = reader.read_next()
        observed[topic] = observed.get(topic, 0) + 1
    del reader
    if observed != counts:
        raise RuntimeError("合并后 MCAP 消息计数校验失败")
    write_json(temporary / "distributed_session.json", session)
    write_json(temporary / "time_alignment.json", report)
    write_json(temporary / "recording_manifest.json", {"format": "rosbag2", "storage_id": "mcap", "distributed": True, "session_id": session["session_id"], "raw_parts": [str(wrists), str(ego)]})
    os.replace(temporary, output)
    return report
