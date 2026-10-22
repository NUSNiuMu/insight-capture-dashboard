"""Child-process UVC reader: camera MJPEG passthrough with kernel timestamps.

Run as ``python3 -m insight_capture.aruco_capture.v4l2_reader DEVICE WIDTH
HEIGHT FPS``. Writes one record per frame to stdout: little-endian int64
capture timestamp (CLOCK_MONOTONIC ns, i.e. the same clock as the parent's
``time.monotonic()``), uint32 payload length, then the camera's own JPEG.

The timestamp is the V4L2 buffer timestamp uvcvideo stamps at the start of
the frame transfer. Taking it here instead of when the parent finishes
reading a pipe avoids the 0–27 ms delivery jitter the old ffmpeg
decode/re-encode pipeline added. Exits non-zero when the camera stops
delivering, so the parent's reconnect loop takes over; running in a child
process still bounds any V4L2 driver stall to a killable process.
"""

import struct
import sys
import time

import cv2

HEADER = struct.Struct("<qI")


def main(device, width, height, fps):
    capture = cv2.VideoCapture(device, cv2.CAP_V4L2)
    if not capture.isOpened():
        sys.exit(f"无法打开 {device}")
    capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    if width and height:
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    if fps:
        capture.set(cv2.CAP_PROP_FPS, fps)
    # Hand back the compressed buffer untouched instead of decoding to BGR.
    capture.set(cv2.CAP_PROP_CONVERT_RGB, 0)
    out = sys.stdout.buffer
    while capture.grab():
        ok, buffer = capture.retrieve()
        if not ok or buffer is None:
            break
        jpeg = buffer.tobytes()
        if not jpeg.startswith(b"\xff\xd8"):
            sys.exit("相机未输出 MJPEG，请检查采集格式")
        stamp_ms = capture.get(cv2.CAP_PROP_POS_MSEC)
        stamp_ns = int(stamp_ms * 1e6) if stamp_ms > 0 else time.monotonic_ns()
        out.write(HEADER.pack(stamp_ns, len(jpeg)))
        out.write(jpeg)
        out.flush()
    sys.exit("相机断开或无法读取，请检查权限、占用和采集格式")


if __name__ == "__main__":
    device, width, height, fps = sys.argv[1:5]
    try:
        main(device, int(width), int(height), float(fps))
    except BrokenPipeError:
        pass
