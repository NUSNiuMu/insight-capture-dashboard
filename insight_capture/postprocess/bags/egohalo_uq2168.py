"""Build a private head-only map and localize both UQ2168 wrists from one merged Take."""

import argparse
from collections import Counter
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import struct
import time

import cv2
import numpy as np
from rclpy.serialization import deserialize_message
from rclpy.time import Time
from sensor_msgs.msg import CameraInfo, CompressedImage
from geometry_msgs.msg import PoseStamped
from std_msgs.msg import String, UInt8MultiArray, UInt64MultiArray
from tf2_msgs.msg import TFMessage
from tf2_ros import Buffer

from insight_capture.core.bag import MANIFEST_NAME, iter_messages
from insight_capture.core.paths import PROJECT_ROOT
from insight_capture.media.h264_decode import H264Decoder
from insight_capture.runtime.camera_profiles import ROLES, UQ2168_MODE
from insight_capture.runtime.mapping.fisheye import FisheyeRectification
from insight_capture.runtime.mapping.geometry import matrix_from_transform
from insight_capture.runtime.mapping.looper_backend import LooperBackend
from insight_capture.runtime.mapping.looper_features import stereo_arrays
from insight_capture.runtime.mapping.rig import calibration_identity
from insight_capture.runtime.mapping.superglue_backend import IpcSuperGlueBackend
from insight_capture.runtime.mapping.synchronization import StereoPairSynchronizer, select_timestamp
from insight_capture.runtime.recording.distributed_merge import header_ns

BASE = "/egohalo/camera/"
WRISTS = {role: f"/uvc_{role}/camera/image_raw/compressed" for role in ROLES}
HEAD_CONFIG = PROJECT_ROOT / "config/devices/jetson-nx-egohalo/cameras.json"


def matrix(position, quaternion):
    return matrix_from_transform(
        (position.x, position.y, position.z),
        (quaternion.x, quaternion.y, quaternion.z, quaternion.w),
    )


def camera_model(size, k, imu_from_camera):
    return {
        "width": size[0], "height": size[1],
        "intrinsics": [k[0, 0], k[1, 1], k[0, 2], k[1, 2]],
        "imu_from_camera": imu_from_camera.tolist(),
    }


def head_inputs(take, streams, imu_frame, start, end):
    expected = {stream["camera_info_topic"] for stream in streams}
    infos, poses, transforms = {}, {}, {}
    tf = Buffer()
    tracked = False
    for topic, raw, when in iter_messages(take, {*expected, "/tf_static", BASE + "vio_image", BASE + "vio_status"}):
        if topic == "/tf_static":
            for item in deserialize_message(raw, TFMessage).transforms:
                key = (item.header.frame_id, item.child_frame_id)
                if key in transforms and transforms[key] != item.transform:
                    raise ValueError(f"Static transform changed during Take: {key}")
                transforms[key] = item.transform
                tf.set_transform_static(item, "recorded")
        elif topic in expected:
            item = deserialize_message(raw, CameraInfo)
            if topic in infos and calibration_identity([infos[topic]], []) != calibration_identity([item], []):
                raise ValueError(f"Head calibration changed during Take: {topic}")
            infos[topic] = item
        elif topic == BASE + "vio_status" and start <= when <= end:
            status = deserialize_message(raw, String).data
            if tracked and status in {"NOT_INITED", "NO_CALIBRATION", "RESTARTING", "STOPPED"}:
                raise ValueError(f"EgoHalo VIO reset during common interval: {status}")
            tracked |= status in {"TRACKING", "TRACKING_STATIC"}
        elif topic == BASE + "vio_image" and start <= header_ns(raw) <= end:
            pose = deserialize_message(raw, PoseStamped).pose
            poses[header_ns(raw)] = matrix(pose.position, pose.orientation)

    if set(infos) != expected or not poses:
        raise ValueError("Take requires both EgoHalo raw CameraInfo streams and same-exposure VIO")
    left, right = [infos[stream["camera_info_topic"]] for stream in streams]

    def lookup(target, source):
        value = tf.lookup_transform(target, source, Time()).transform
        return matrix(value.translation, value.rotation)

    right_from_left = lookup(right.header.frame_id, left.header.frame_id)
    imu_from_left = lookup(imu_frame, left.header.frame_id)
    return (left, right), right_from_left, imu_from_left, poses


def stereo_images(take, streams, hardware):
    names = [stream["topic"] for stream in streams]
    pairs = StereoPairSynchronizer(tolerance_ns=2_000_000, queue_size=24)

    def drain(eye, decoder, wait):
        result = decoder.pull(wait)
        while result is not None:
            push = pairs.push_left if eye == 0 else pairs.push_right
            pair = push(*result)
            if pair is not None:
                yield pair
            result = decoder.pull()

    with ExitStack() as stack:
        decoders = []
        for _ in names:
            decoder = H264Decoder(hardware=hardware, pixel_format="GRAY8")
            stack.callback(decoder.close)
            decoders.append(decoder)
        for name, raw, _ in iter_messages(take, names):
            eye = names.index(name)
            decoder = decoders[eye]
            msg = deserialize_message(raw, CompressedImage)
            if msg.format.lower() != "h264":
                raise ValueError(f"{name}: expected H.264 access units")
            if decoder.push(bytes(msg.data), header_ns(raw)):
                yield from drain(eye, decoder, 10_000_000)
        for decoder in decoders:
            decoder.finish()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            pending = False
            for eye, decoder in enumerate(decoders):
                if not decoder.sink.get_property("eos"):
                    pending = True
                    yield from drain(eye, decoder, 50_000_000)
            if not pending:
                break
        else:
            raise RuntimeError("Timed out draining EgoHalo stereo decoders")


def wrist_geometry(path, degrees, size):
    raw = path.read_bytes()
    info = json.loads(raw)["camera_calibrations"][0]["intrinsics"]
    width, height, _ = UQ2168_MODE
    if info["camera_model"].upper() != "FISHEYE" or (info["width"], info["height"]) != (width, height):
        raise ValueError(f"Expected UQ2168 fisheye calibration at {width}×{height}: {path}")
    k = np.array([[info["fx"], 0, info["cx"]], [0, info["fy"], info["cy"]], [0, 0, 1]], dtype=float)
    d = np.asarray(info["distortion_coef"], dtype=float).reshape(4)
    if not np.isfinite(k).all() or not np.isfinite(d).all() or min(k[0, 0], k[1, 1]) <= 0:
        raise ValueError(f"Invalid UQ2168 intrinsics: {path}")
    f = min(size) / 2
    new_k = np.array([[f, 0, (size[0] - 1) / 2], [0, f, (size[1] - 1) / 2], [0, 0, 1]])
    virtual_from_optical = np.eye(4)
    if degrees == 180:
        virtual_from_optical[:2, :2] *= -1
    maps = cv2.fisheye.initUndistortRectifyMap(k, d, virtual_from_optical[:3, :3], new_k, size, cv2.CV_16SC2)
    return camera_model(size, new_k, np.eye(4)), maps, virtual_from_optical, hashlib.sha256(raw).hexdigest()


def wrist_audit(take, start, end):
    result = {}
    for role in ROLES:
        prefix = f"/uvc_{role}/camera/"
        image_topic = WRISTS[role]
        timing_topic, imu_topic, meta_topic = [prefix + leaf for leaf in ("frame_timing", "imu_raw", "uvc_metadata")]
        images, timings, ticks = Counter(), Counter(), set()
        counts = Counter()
        gaps = resets = 0
        last_seq = None
        offsets = set()
        imu_updates = []
        for topic, raw, receipt in iter_messages(take, (image_topic, timing_topic, imu_topic, meta_topic)):
            if topic == image_topic:
                stamp = header_ns(raw)
                if start <= stamp <= end:
                    images[stamp] += 1
            elif topic == timing_topic:
                fields = deserialize_message(raw, UInt64MultiArray).data
                if len(fields) != 4:
                    counts["invalid_timing"] += 1
                    continue
                seq, mono, arrived, stamp = fields
                if not start <= stamp <= end:
                    continue
                offsets.add(stamp - mono)
                timings[stamp] += 1
                if seq > 0xFFFFFFFF or arrived < mono:
                    counts["invalid_timing"] += 1
                if last_seq is not None:
                    delta = (seq - last_seq) % (1 << 32)
                    if 0 < delta < (1 << 31):
                        gaps += delta - 1
                    else:
                        resets += 1
                last_seq = seq
            elif topic == imu_topic and start <= receipt <= end:
                data = bytes(deserialize_message(raw, UInt8MultiArray).data)
                if len(data) != 144:
                    counts["invalid_imu"] += 1
                    continue
                sent, received = struct.unpack_from("<QQ", data)
                if sent > received or not all(data[i:i + 2] == b"\xde\xc8" for i in range(16, 142, 18)):
                    counts["invalid_imu"] += 1
                    continue
                counts["imu_packets"] += 1
                packet_ticks = {struct.unpack_from(">I", data, 18 + i)[0] for i in (0, 18, 36, 72, 90, 108)}
                if packet_ticks - ticks:
                    imu_updates.append(receipt)
                ticks.update(packet_ticks)
            elif topic == meta_topic:
                counts["metadata_packets"] += 1
        count = sum(images.values())
        times = sorted(images)
        imu_stamps = [start, *imu_updates, end]
        imu_gap = max(b - a for a, b in zip(imu_stamps, imu_stamps[1:]))
        result[role] = {
            "ok": (len(times) > 1 and count == len(times) and images == timings and len(imu_updates) > 1
                   and imu_gap <= 500_000_000 and not (counts["invalid_timing"] or counts["invalid_imu"] or resets)),
            "frames": count, "actual_fps": (count - 1) * 1e9 / (times[-1] - times[0]) if len(times) > 1 else 0,
            "max_image_gap_ns": max((b - a for a, b in zip(times, times[1:])), default=0),
            "missing_sequence_frames": gaps, "sequence_resets": resets,
            "image_without_timing": sum((images - timings).values()),
            "timing_without_image": sum((timings - images).values()),
            "imu_unique_ticks": len(ticks), "max_imu_update_gap_ns": imu_gap,
            "epoch_minus_monotonic_ns": sorted(offsets),
            **{key: counts[key] for key in ("imu_packets", "metadata_packets", "invalid_timing", "invalid_imu")},
        }
    return result


def localize(take, calibrations, rotations, mapper, matcher, *, hardware=True):
    alignment = json.loads((take / "time_alignment.json").read_text())
    start, end = alignment["common_start_ns"], alignment["common_end_ns"]
    if not set(WRISTS.values()).issubset(alignment["video_decode_start_ns"]):
        raise ValueError("Take is missing one or both recorded UQ2168 wrist streams")
    report = {
        "session_id": alignment["session_id"], "clock_ok": alignment["clock_ok"],
        "frame": "egohalo_map", "common_ns": [start, end], "ok": False,
        "capture": wrist_audit(take, start, end),
        "wrists": {role: [] for role in ROLES},
        "verified_counts": dict.fromkeys(ROLES, 0),
    }
    manifest = json.loads((take / MANIFEST_NAME).read_text())
    config = manifest.get("camera_configuration") or json.loads(HEAD_CONFIG.read_text())
    report["wrist_bindings"] = {
        camera["name"].removeprefix("uvc_"): camera["uvc_binding"]
        for camera in config["cameras"] if camera.get("uvc_binding")
    }
    settings = config["egohalo_rectification"]
    size = (settings["output_width"], settings["output_height"])
    streams_by_name = {item["name"]: item for camera in config["cameras"] for item in camera.get("dashboard_streams", [])}
    front = next(view for view in config["mapping"]["stereo_views"] if view["name"] == "front")
    streams = [streams_by_name[front[side + "_source"]] for side in ROLES]
    infos, right_from_left, imu_from_left, poses = head_inputs(take, streams, config["mapping"]["imu_frame"], start, end)
    rectification = FisheyeRectification.create(*infos, right_from_left, size=size,
                                               horizontal_fov_deg=settings["horizontal_fov_deg"])
    models = []
    for projection, rotation, extrinsic in zip(rectification.projections, rectification.rotations,
                                               (imu_from_left, imu_from_left @ np.linalg.inv(right_from_left))):
        rect_to_raw = np.eye(4)
        rect_to_raw[:3, :3] = rotation.T
        models.append(camera_model(size, projection[:, :3], extrinsic @ rect_to_raw))

    mapper.request("initialize", cameras=models)
    head_count = next_ns = 0
    for pair in stereo_images(take, streams, hardware):
        ts = pair.left_stamp_ns
        if ts not in poses:
            continue
        use, next_ns = select_timestamp(ts, next_ns, 2.0)
        if use:
            images = [rectification.rectify(0, pair.left), rectification.rectify(1, pair.right)]
            mapper.request("map", images=images, arrays=stereo_arrays(*images, matcher.match(*images)),
                           stamp_ns=ts, tracking_epoch=0, odom_from_imu=poses[ts].tolist())
            head_count += 1
    report["head_frames_submitted"] = head_count
    report["map"] = mapper.request("stats")
    if not report["map"]["localization_ready"]:
        report["reason"] = "head_map_not_ready"
        return report

    wrist_models = {}
    for camera_id, role in enumerate(ROLES, 2):
        model, maps, transform, digest = wrist_geometry(calibrations[role], rotations[role], size)
        mapper.request("camera", camera_id=camera_id, camera=model)
        wrist_models[WRISTS[role]] = (camera_id, role, maps, transform)
        report["capture"][role].update(calibration_sha256=digest, rotation_deg=rotations[role])
    next_stamp = dict.fromkeys(ROLES, 0)
    verified = {role: [] for role in ROLES}
    width, height, _ = UQ2168_MODE
    for topic, raw, _ in iter_messages(take, wrist_models):
        ts = header_ns(raw)
        if not start <= ts <= end:
            continue
        camera_id, role, maps, transform = wrist_models[topic]
        use, next_stamp[role] = select_timestamp(ts, next_stamp[role], 4.0)
        if not use:
            continue
        msg = deserialize_message(raw, CompressedImage)
        if msg.format.lower() != "jpeg":
            raise ValueError(f"{role}: expected original JPEG at {ts}")
        image = cv2.imdecode(np.frombuffer(msg.data, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if image is None or image.shape != (height, width):
            raise ValueError(f"{role}: invalid UQ2168 JPEG at {ts}")
        rect = cv2.remap(image, *maps, cv2.INTER_LINEAR)
        features = matcher.extract(rect)
        result = mapper.request("localize", images=[rect], arrays=[features.keypoints, features.descriptors],
                                stamp_ns=ts, camera_id=camera_id, tracking_epoch=0, odom_from_imu=np.eye(4).tolist())
        accepted = bool(result.get("localized") and result.get("verified_relocalization"))
        sample = {"stamp_ns": ts, "verified": accepted,
                  **{key: result[key] for key in ("reason", "matches", "inliers", "reprojection_rms_px") if key in result}}
        if accepted:
            sample["map_from_optical"] = (np.asarray(result["map_from_imu"]) @ transform).tolist()
            verified[role].append(ts)
        report["wrists"][role].append(sample)

    report["verified_counts"] = {role: len(values) for role, values in verified.items()}
    left, right = (np.asarray(sorted(verified[role]), dtype=np.int64) for role in ROLES)
    simultaneous = 0
    if left.size and right.size:
        indices = np.searchsorted(right, left)
        distances = np.minimum(abs(left - right[np.maximum(0, indices - 1)]),
                               abs(left - right[np.minimum(right.size - 1, indices)]))
        simultaneous = int(np.count_nonzero(distances <= 50_000_000))
    report["left_queries_with_right_within_50ms"] = simultaneous
    report["ok"] = bool(simultaneous and report["clock_ok"] and all(item["ok"] for item in report["capture"].values()))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("take", type=Path)
    parser.add_argument("--mapper", type=Path, required=True, help="Socket of a private insight_mapping process")
    parser.add_argument("--matcher", type=Path, required=True)
    parser.add_argument("--software-decode", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    for role in ROLES:
        parser.add_argument(f"--{role}-calibration", type=Path, required=True)
        parser.add_argument(f"--{role}-rotation", type=int, choices=(0, 180), default=0)
    args = parser.parse_args()
    mapper = LooperBackend(args.mapper)
    try:
        result = localize(args.take, {role: getattr(args, role + "_calibration") for role in ROLES},
                          {role: getattr(args, role + "_rotation") for role in ROLES},
                          mapper, IpcSuperGlueBackend(args.matcher),
                          hardware=not args.software_decode)
    finally:
        mapper.close()
    with args.output.open("x") as handle:
        json.dump(result, handle, indent=2)
    raise SystemExit(0 if result["ok"] else 2)


if __name__ == "__main__":
    main()
