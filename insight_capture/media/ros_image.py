"""Decode recorded ROS image messages for media and geometry tools."""

from typing import Optional

import cv2
import numpy as np

IMAGE_TYPES = {"sensor_msgs/msg/Image", "sensor_msgs/msg/CompressedImage"}

def _decode_image(
    message: object,
    message_type: str,
) -> Optional[np.ndarray]:
    """Decode supported ROS image messages into contiguous BGR or mono pixels."""

    if message_type == "sensor_msgs/msg/CompressedImage":
        return cv2.imdecode(
            np.frombuffer(message.data, dtype=np.uint8), cv2.IMREAD_COLOR
        )
    if message_type != "sensor_msgs/msg/Image":
        raise ValueError(f"unsupported image message type: {message_type}")

    height = int(message.height)
    width = int(message.width)
    step = int(message.step)
    if height <= 0 or width <= 0 or step <= 0:
        return None
    raw = np.frombuffer(message.data, dtype=np.uint8)
    encoding = str(message.encoding).lower()
    if encoding in {"mono8", "8uc1"}:
        if step < width or raw.size < height * step:
            return None
        return np.ascontiguousarray(
            raw[: height * step].reshape(height, step)[:, :width]
        )
    if encoding in {"bgr8", "rgb8"}:
        row_bytes = width * 3
        if step < row_bytes or raw.size < height * step:
            return None
        image = raw[: height * step].reshape(height, step)[:, :row_bytes]
        image = np.ascontiguousarray(image.reshape(height, width, 3))
        if encoding == "rgb8":
            return cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        return image
    if encoding in {"bgra8", "rgba8"}:
        row_bytes = width * 4
        if step < row_bytes or raw.size < height * step:
            return None
        image = raw[: height * step].reshape(height, step)[:, :row_bytes]
        image = np.ascontiguousarray(image.reshape(height, width, 4))
        conversion = (
            cv2.COLOR_BGRA2BGR if encoding == "bgra8" else cv2.COLOR_RGBA2BGR
        )
        return cv2.cvtColor(image, conversion)
    if encoding == "nv12":
        expected_rows = height * 3 // 2
        if (
            width % 2
            or height % 2
            or step < width
            or raw.size < expected_rows * step
        ):
            return None
        yuv = np.ascontiguousarray(
            raw[: expected_rows * step].reshape(expected_rows, step)[:, :width]
        )
        return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_NV12)
    raise ValueError(f"unsupported image encoding: {message.encoding}")


def decode_color_image(message: object, message_type: str) -> Optional[np.ndarray]:
    """Decode complete color pixels for dataset and media output."""

    return _decode_image(message, message_type)


def header_stamp_ns(message: object) -> int:
    stamp = message.header.stamp
    return int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)
