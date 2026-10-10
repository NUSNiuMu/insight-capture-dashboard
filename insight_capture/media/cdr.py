"""Read compressed-image headers without materializing a Python pixel array."""

import struct
from types import SimpleNamespace


def compressed_image_view(raw):
    """Return a lightweight sensor_msgs/CompressedImage view over XCDR1 bytes."""
    data = memoryview(raw)
    if len(data) < 12 or bytes(data[:2]) not in (b"\x00\x00", b"\x00\x01"):
        raise ValueError("Unsupported compressed-image CDR encapsulation")
    order = "<" if data[1] else ">"
    sec, nanosec = struct.unpack_from(order + "iI", data, 4)
    offset = 12

    def string():
        nonlocal offset
        offset = (offset + 3) & ~3
        if offset + 4 > len(data):
            raise ValueError("Truncated compressed-image CDR header")
        length, = struct.unpack_from(order + "I", data, offset)
        offset += 4
        if not length or offset + length > len(data) or data[offset + length - 1] != 0:
            raise ValueError("Invalid compressed-image CDR string")
        value = bytes(data[offset:offset + length - 1]).decode("utf-8")
        offset += length
        return value

    frame_id, fmt = string(), string()
    offset = (offset + 3) & ~3
    if offset + 4 > len(data):
        raise ValueError("Missing compressed-image CDR data length")
    length, = struct.unpack_from(order + "I", data, offset)
    offset += 4
    if offset + length > len(data):
        raise ValueError("Truncated compressed-image CDR data")
    return SimpleNamespace(
        header=SimpleNamespace(stamp=SimpleNamespace(sec=sec, nanosec=nanosec), frame_id=frame_id),
        format=fmt, data=data[offset:offset + length],
    )
