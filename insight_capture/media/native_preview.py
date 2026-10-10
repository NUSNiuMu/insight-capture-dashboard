"""Bounded local Qt preview queues sharing the existing ROS image readers."""

from __future__ import annotations

import asyncio
import struct
import threading
import time

from .h264 import H264Queue
from .rate import select_frame

# Versioned network-order header: magic, version, format, reserved, width,
# height, source timestamp and host receipt time. Payload is one complete frame.
HEADER = struct.Struct("!4sBBHIIQQ")
FORMATS = {"GRAY8": 1, "NV12": 2, "JPEG": 3, "H264": 4}


def image_packet(message, topic_type, width, height, received_ns):
    """Pack one ROS image without decoding or encoding its pixels."""
    stamp = int(message.header.stamp.sec) * 1_000_000_000 + int(message.header.stamp.nanosec)
    if topic_type in ("compressed", "h264"):
        fmt = "H264" if topic_type == "h264" else "JPEG"
        data = bytes(message.data)
    else:
        width, height, step = int(message.width), int(message.height), int(message.step)
        encoding = str(message.encoding).lower()
        fmt = {"mono8": "GRAY8", "8uc1": "GRAY8", "nv12": "NV12"}.get(encoding)
        if fmt is None or width <= 0 or height <= 0 or step < width:
            raise ValueError("Unsupported native image layout")
        data = bytes(message.data)
        rows = height
        if fmt == "NV12":
            rows, remainder = divmod(len(data), step)
            if remainder or rows <= 0 or rows % 3:
                raise ValueError("Invalid NV12 plane layout")
            # Some Insight drivers report all YUV rows as Image.height.
            height = rows * 2 // 3
        if len(data) < rows * step:
            raise ValueError("Truncated native image")
        if step != width:
            data = b"".join(data[row * step:row * step + width] for row in range(rows))
        else:
            data = data[:rows * width]
    return HEADER.pack(b"INPV", 1, FORMATS[fmt], 0, width, height, stamp, received_ns) + data


class NativePreviewClient:
    def __init__(self, spec, fps, loop):
        self.spec, self.fps, self.loop = spec, fps, loop
        self.event = asyncio.Event()
        self.lock = threading.Lock()
        self.closed = False
        self.received = self.selected_packets = self.replaced = self.expired = 0
        self.latest = None
        self.next_frame_at = 0.0
        self.h264 = H264Queue(max_age_sec=0.15) if spec.topic_type == "h264" else None

    def offer(self, message):
        with self.lock:
            if self.closed:
                return
            self.received += 1
            if self.h264 is not None:
                stamp = int(message.header.stamp.sec) * 1_000_000_000 + int(message.header.stamp.nanosec)
                if not self.h264.push(bytes(message.data), stamp):
                    return
            else:
                self.replaced += int(self.latest is not None)
                self.latest = (message, time.monotonic_ns())
        try:
            self.loop.call_soon_threadsafe(self.event.set)
        except RuntimeError:
            self.close()

    def packets(self):
        with self.lock:
            if self.closed:
                return []
            if self.h264 is not None:
                packets = [HEADER.pack(b"INPV", 1, FORMATS["H264"], 0,
                                    self.spec.width, self.spec.height, stamp or 0, received) + data
                        for data, stamp, received in self.h264.drain_with_metadata()]
                self.selected_packets += len(packets)
                return packets
            latest, self.latest = self.latest, None
        if latest is None:
            return []
        message, received = latest
        now = time.monotonic()
        selected, self.next_frame_at = select_frame(now, self.next_frame_at, self.fps)
        expired = time.monotonic_ns() - received > 150_000_000
        if not selected or expired:
            self.expired += int(expired)
            return []
        self.selected_packets += 1
        return [image_packet(message, self.spec.topic_type, self.spec.width, self.spec.height, received)]

    def close(self):
        with self.lock:
            self.closed = True
            self.latest = None
            if self.h264 is not None:
                self.h264.reset()


class NativePreviewHub:
    def __init__(self, cameras):
        self.specs = {camera.name: camera for camera in cameras}
        self._lock = threading.Lock()
        self._clients = {}

    def connect(self, name, fps, loop):
        client = NativePreviewClient(self.specs[name], fps, loop)
        with self._lock:
            self._clients.setdefault(name, set()).add(client)
        return client

    def disconnect(self, client):
        with self._lock:
            self._clients.get(client.spec.name, set()).discard(client)
        client.close()

    def active(self, name):
        with self._lock:
            return bool(self._clients.get(name))

    def submit(self, name, message):
        with self._lock:
            clients = tuple(self._clients.get(name, ()))
        for client in clients:
            client.offer(message)

    def status(self, name):
        with self._lock:
            clients = tuple(self._clients.get(name, ()))
        result = {"sessions": len(clients), "received": 0, "selected_packets": 0, "replaced": 0, "expired": 0}
        for client in clients:
            with client.lock:
                for key in ("received", "selected_packets", "replaced", "expired"):
                    result[key] += getattr(client, key)
                if client.h264 is not None:
                    result["h264"] = client.h264.stats()
        return result
