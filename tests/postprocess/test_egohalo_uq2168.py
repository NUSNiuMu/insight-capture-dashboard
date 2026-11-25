"""Exercise one fixture Take through the distributed coordinator, six-stream review and offline map."""

from array import array
from pathlib import Path
from types import SimpleNamespace
import json
import os
import shutil
import struct
import subprocess
import threading
import time

import cv2
import numpy as np
import pytest

bag = pytest.importorskip("rosbag2_py")
pytest.importorskip("gi")

from geometry_msgs.msg import PoseStamped, TransformStamped
from rclpy.serialization import serialize_message
from sensor_msgs.msg import CameraInfo, CompressedImage
from std_msgs.msg import UInt8MultiArray, UInt64MultiArray
from tf2_msgs.msg import TFMessage

from insight_capture.postprocess.bags import egohalo_uq2168 as offline
from insight_capture.postprocess.bags import h264_review, synchronization
from insight_capture.runtime import camera_profiles, uq2168
from insight_capture.runtime.recording import distributed
from insight_capture.runtime.recording.distributed_merge import merge_session
from insight_capture.runtime.recording.recorder import build_default_topics


def write_bag(path, rows):
    writer = bag.SequentialWriter()
    writer.open(bag.StorageOptions(uri=str(path), storage_id="mcap"), bag.ConverterOptions("", ""))
    types = {}
    for topic, stamp, msg in sorted(rows, key=lambda row: row[1]):
        if topic not in types:
            package = type(msg).__module__.split(".")[0]
            types[topic] = type(msg).__name__
            writer.create_topic(bag.TopicMetadata(name=topic, type=f"{package}/msg/{types[topic]}",
                                                  serialization_format="cdr"))
        if hasattr(msg, "header"):
            msg.header.stamp.sec, msg.header.stamp.nanosec = divmod(stamp, 10**9)
        writer.write(topic, serialize_message(msg), stamp)
    del writer


def test_native_packet_stream_reassembles_wire_bytes():
    read_fd, write_fd = os.pipe()
    try:
        jpeg = b"camera JPEG bytes"
        imu = bytes(range(128))
        wire = uq2168.PACKET.pack(1, len(jpeg), 20, 9) + jpeg + uq2168.PACKET.pack(2, len(imu), 24, 21) + imu
        os.write(write_fd, wire)
        os.close(write_fd)
        write_fd = None
        assert list(uq2168.packets(read_fd, threading.Event())) == [(1, 20, 9, jpeg), (2, 24, 21, imu)]
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)


def test_same_take_egohalo_uq2168(tmp_path, monkeypatch):
    if shutil.which("ffmpeg") is None:
        pytest.skip("requires ffmpeg and libx264")
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    if Gst.ElementFactory.find("avdec_h264") is None:
        pytest.skip("requires the GStreamer software H.264 decoder")

    config = tmp_path / "config"
    shutil.copytree(camera_profiles.ROOT / "config" / "devices", config / "devices")
    (config / ".device").write_text("jetson-nx\n")
    usb = {role: {"product": camera_profiles.UQ2168_PRODUCT, "serial": role, "port": str(i)}
           for i, role in enumerate(camera_profiles.ROLES)}
    peer = {"namespace": "egohalo", "host": "169.254.10.1", "address": "169.254.10.2", "index": 1}
    (config / camera_profiles.BINDINGS).write_text(json.dumps(
        {role: {"product": item["product"], "serial": item["serial"]} for role, item in usb.items()}))
    candidate = camera_profiles.detect([peer], usb)
    settings = camera_profiles.render(config, candidate, {})
    video = settings["distributed_recording"]
    assert len(video["image_topics"]) == 6
    assert set(video["required_aux_topics"]).issubset(build_default_topics(settings))
    assert list(video["image_rates"].values()) == [90, 90]
    assert [cam["uq2168"] for cam in settings["cameras"][:2]] == [True, True]
    video["keyframe_lead_in_sec"] = 0

    start = time.time_ns()
    encoded = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=30", "-frames:v", "1",
         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-threads", "1", "-f", "h264", "pipe:1"],
        check=True, stdout=subprocess.PIPE,
    ).stdout
    jpeg = array("B", cv2.imencode(".jpg", np.full((1200, 1920), 80, dtype=np.uint8))[1].tobytes())
    head_rows = []
    head = next(item for item in settings["cameras"] if item["name"] == "egohalo")
    for index, stream in enumerate(head["dashboard_streams"]):
        frame = stream["camera_info_topic"].removesuffix("/camera_info").replace("/", "_")[1:]
        info = CameraInfo(width=64, height=64, distortion_model="equidistant", d=[0.0] * 4,
                          k=[32., 0., 31.5, 0., 32., 31.5, 0., 0., 1.])
        info.header.frame_id = frame
        head_rows.append((stream["camera_info_topic"], start, info))
        tf = TransformStamped(child_frame_id=frame)
        tf.header.frame_id = settings["mapping"]["imu_frame"]
        tf.transform.translation.x = index * 0.08
        tf.transform.rotation.w = 1.0
        head_rows.append(("/tf_static", start, TFMessage(transforms=[tf])))
        for i in range(36):
            msg = CompressedImage(format="h264", data=array("B", encoded))
            msg.header.frame_id = frame
            head_rows.append((stream["topic"], start + i * 10**9 // 30, msg))
    for i in range(36):
        pose = PoseStamped()
        pose.pose.position.x = i * 0.005
        pose.pose.orientation.w = 1.0
        head_rows.append((offline.BASE + "vio_image", start + i * 10**9 // 30, pose))

    wrist_rows = []
    offset = start - 10**12
    for role, topic in offline.WRISTS.items():
        prefix = f"/uvc_{role}/camera/"
        for i in range(105):
            stamp = start + i * 10**9 // 90
            mono = stamp - offset
            wrist_rows.extend([
                (topic, stamp, CompressedImage(format="jpeg", data=jpeg)),
                (prefix + "frame_timing", stamp, UInt64MultiArray(data=[i, mono, mono + 5000, stamp])),
            ])
            packet = bytearray(128)
            for element in range(7):
                struct.pack_into(">HI", packet, element * 18, 0xDEC8, i * 7 + element)
            packet_data = array("B", struct.pack("<QQ", mono - 100, mono) + packet)
            wrist_rows.append((prefix + "imu_raw", stamp, UInt8MultiArray(data=packet_data)))
        wrist_rows.append((prefix + "uvc_metadata", start, UInt8MultiArray(data=[1, 2, 3])))

    class Local:
        rosbag_root = tmp_path
        default_topics = build_default_topics(settings)
        processes = {}
        active = False
        _lock = threading.RLock()

        def is_recording(self):
            return self.active

        def status(self):
            return {"recording": self.active}

        def _refresh_recording_storage_unlocked(self):
            pass

        def _cleanup_if_exited_unlocked(self):
            pass

        def add_recording_completed_callback(self, callback):
            pass

        def storage_browse_roots(self):
            return [self.rosbag_root]

        def start(self, topics, name, mode, root, **_kwargs):
            assert set(video["required_aux_topics"]).issubset(topics)
            self.output_path = str(self.rosbag_root / root / name)
            self.active = True

        def stop(self, *_args):
            path = Path(self.output_path)
            write_bag(path, wrist_rows)
            (path / "recording_manifest.json").write_text(json.dumps({"camera_configuration": settings}))
            self.active = False
            return {"merge_state": "done"}

    class Ego:
        base = video["ego_url"]
        timeout = 1

        def __init__(self, _config):
            self.record = {}

        def preflight(self):
            return {"storage": {"media": {"token": "fixture"}}}

        def task(self, *_args):
            return "fixture"

        def request(self, route, body, **_kwargs):
            assert route == "/api/recording/start"
            self.sid = body["session_id"]
            self.record = {"id": "fixture", "status": "recording", "path": "fixture"}

        def lookup(self, session):
            assert session["session_id"] == self.sid
            return self.record

        def cancel(self, session):
            self.lookup(session)["status"] = "complete"
            return self.record

        def download(self, session, output):
            assert session["session_id"] == self.sid
            write_bag(output, head_rows)
            return {"fixture": True}

    def wait_for(status, key, state):
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            value = status()
            if value[key] in (state, "error"):
                assert value[key] == state, value
                return value
            time.sleep(0.05)
        pytest.fail(f"Timed out waiting for {state}: {status()}")

    monkeypatch.setattr(distributed, "EgoParticipant", Ego)
    monkeypatch.setattr(distributed, "measure_clocks", lambda *_args, **_kwargs: {"ok": True})
    coordinator = distributed.DistributedRecordingManager(Local(), video, tmp_path / "state.json")
    started = coordinator.start(bag_name="take", require_global_pose=False)
    sid = started["distributed"]["session_id"]
    assert coordinator.ego.sid == sid
    coordinator.stop()
    saved = wait_for(coordinator.status, "merge_state", "saved")
    assert saved["distributed"]["local_saved"] and saved["distributed"]["remote_saved"]
    coordinator.start_merge(sid)
    wait_for(coordinator.status, "merge_state", "done")
    take = tmp_path / "take"
    time_alignment = json.loads((take / "time_alignment.json").read_text())
    assert time_alignment["session_id"] == sid
    assert set(time_alignment["video_decode_start_ns"]) == set(video["image_topics"])
    assert all(item["ok"] for item in offline.wrist_audit(
        take, time_alignment["common_start_ns"], time_alignment["common_end_ns"]).values())
    raw_ego = Path(coordinator._session["ego_copy_path"])
    raw_wrists = Path(coordinator._session["jetson_path"])
    new_required = {**video, "required_aux_topics": ["/uvc_left/camera/missing_fixture"]}
    with pytest.raises(RuntimeError, match="missing_fixture"):
        merge_session(coordinator._session, new_required, raw_ego, raw_wrists, tmp_path / "incomplete")
    assert raw_ego.is_dir() and raw_wrists.is_dir() and not (tmp_path / "incomplete").exists()

    class SoftwareWriter:
        encoder_name = "libx264"

        def __init__(self, path, shape, fps):
            height, width = shape
            self.frames = 0
            self.process = subprocess.Popen(
                ["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{width}x{height}",
                 "-r", str(fps), "-i", "pipe:0", "-threads", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
                stdin=subprocess.PIPE,
            )

        def write(self, frame):
            self.process.stdin.write(frame.tobytes())
            self.frames += 1

        def close(self):
            self.process.stdin.close()
            assert self.process.wait(timeout=10) == 0

        def abort(self):
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait()

    monkeypatch.setattr(h264_review, "H264Decoder", lambda: offline.H264Decoder(hardware=False))
    monkeypatch.setattr(synchronization, "_GstH264Writer", SoftwareWriter)
    playback = synchronization.PreparedPlaybackManager(tmp_path, tmp_path / "cache")
    playback.prepare("take", coordinator.local, [], [])
    wait_for(playback.status, "state", "ready")
    review_capture = cv2.VideoCapture(str(take / "review/review.mp4"))
    try:
        assert review_capture.read()[0]
    finally:
        review_capture.release()
    manifest = json.loads((take / "review/manifest.json").read_text())
    assert {entry["topic"] for entry in manifest["source_cameras"]} == set(video["image_topics"])

    calibrations = {}
    for role in camera_profiles.ROLES:
        file = tmp_path / f"{role}.json"
        file.write_text(json.dumps({"camera_calibrations": [{"intrinsics": {
            "camera_model": "fisheye", "width": 1920, "height": 1200,
            "fx": 600, "fy": 600, "cx": 960, "cy": 600, "distortion_coef": [0] * 4}}]}))
        calibrations[role] = file

    class SimulatedMapper:
        seen = []

        def request(self, command, **fields):
            self.seen.append((command, fields))
            if command == "stats":
                return {"localization_ready": any(call == "map" for call, _ in self.seen)}
            if command == "localize":
                return {"localized": True, "verified_relocalization": fields["camera_id"] == 3,
                        "map_from_imu": np.eye(4).tolist()}
            return {}

    matcher = SimpleNamespace(
        match=lambda *_: SimpleNamespace(left_points=np.zeros((0, 2)), right_points=np.zeros((0, 2)),
                                         descriptors=np.zeros((0, 256))),
        extract=lambda *_: SimpleNamespace(keypoints=np.zeros((1, 2)), descriptors=np.zeros((1, 256))),
    )
    mapper = SimulatedMapper()
    report = offline.localize(take, calibrations, {"left": 0, "right": 180}, mapper, matcher, hardware=False)
    assert report["session_id"] == sid and report["head_frames_submitted"] >= 2
    assert {role: item["serial"] for role, item in report["wrist_bindings"].items()} == {"left": "left", "right": "right"}
    assert report["verified_counts"]["left"] == 0 < report["verified_counts"]["right"]
    assert not report["ok"]
    assert np.allclose(report["wrists"]["right"][0]["map_from_optical"], np.diag([-1, -1, 1, 1]))
