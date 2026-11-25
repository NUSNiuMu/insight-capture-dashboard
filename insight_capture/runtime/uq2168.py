"""ROS transport for the native UQ2168 video, timing and IMU reader."""

from array import array
from pathlib import Path
import os
import select
import struct
import subprocess
import time

from insight_capture.runtime.camera_profiles import UQ2168_MODE

PACKET = struct.Struct("<IIQQ")


def packets(fd, stop):
    pending = bytearray()
    while not stop.is_set():
        if not select.select([fd], [], [], 0.25)[0]:
            continue
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            if pending:
                raise RuntimeError("Truncated UQ2168 packet")
            return
        pending.extend(chunk)
        offset = 0
        while len(pending) - offset >= PACKET.size:
            kind, size, mono, aux = PACKET.unpack_from(pending, offset)
            if kind not in (1, 2, 3) or not 0 < size <= 16 * 1024 * 1024 or (kind == 2 and size != 128):
                raise RuntimeError("Invalid UQ2168 packet")
            end = offset + PACKET.size + size
            if len(pending) < end:
                break
            yield kind, mono, aux, pending[offset + PACKET.size:end]
            offset = end
        del pending[:offset]


def stream(source, role, publishers, stop):
    from sensor_msgs.msg import CompressedImage
    from std_msgs.msg import UInt8MultiArray, UInt64MultiArray

    video = Path("/sys/class/video4linux") / source.name
    siblings = (video / "device").resolve(strict=True) / "video4linux"
    metadata = [path.name for path in siblings.glob("video*") if (path / "index").read_text().strip() == "1"]
    if len(metadata) != 1:
        raise RuntimeError(f"{source}: expected one UQ2168 metadata node, found {metadata}")
    width, height, fps = UQ2168_MODE
    offset_ns = time.time_ns() - time.monotonic_ns()
    process = subprocess.Popen(
        ["uq2168_stream", str(source), str(source.with_name(metadata[0])), str(fps), str(width), str(height)],
        stdout=subprocess.PIPE,
    )
    try:
        for kind, mono, aux, payload in packets(process.stdout.fileno(), stop):
            if kind == 1:
                utc_ns = mono + offset_ns
                image = CompressedImage(format="jpeg", data=array("B", payload))
                image.header.frame_id = f"uvc_{role}_optical"
                image.header.stamp.sec, image.header.stamp.nanosec = divmod(utc_ns, 10**9)
                publishers["image"].publish(image)
                publishers["frame_timing"].publish(UInt64MultiArray(data=[aux, mono, time.monotonic_ns(), utc_ns]))
            else:
                message = UInt8MultiArray(data=array("B", struct.pack("<QQ", aux, mono) + payload))
                key = "imu_raw" if kind == 2 else "uvc_metadata"
                publishers[key].publish(message)
        if not stop.is_set():
            raise RuntimeError(f"UQ2168 reader exited on {source}")
    finally:
        process.stdout.close()
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
