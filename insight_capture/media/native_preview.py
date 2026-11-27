"""Bounded local Qt preview queues sharing the existing ROS image readers."""

from __future__ import annotations

import asyncio
import struct
import threading
import time

from .h264 import H264Queue
from .jpeg import jpeg_dimensions
from .rate import select_frame

# Versioned network-order header: magic, version, format, reserved, width,
# height, source timestamp and host receipt time. Payload is one complete frame.
HEADER = struct.Struct("!4sBBHIIQQ")
FORMATS = {"GRAY8": 1, "NV12": 2, "JPEG": 3, "H264": 4}
MAX_FRAME_AGE_NS = 150_000_000


def image_packet(message, topic_type, width, height, received_ns):
    """Pack a ROS image for Qt, skipping JPEGs without supported dimensions."""
    stamp = int(message.header.stamp.sec) * 1_000_000_000 + int(message.header.stamp.nanosec)
    if topic_type in ("compressed", "h264"):
        fmt = "H264" if topic_type == "h264" else "JPEG"
        data = bytes(message.data)
        if fmt == "JPEG":
            width, height = jpeg_dimensions(data)
            if not (0 < width <= 8192 and 0 < height <= 8192):
                return None
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
    def __init__(self, spec, fps, loop, *, shared=False):
        self.spec, self.fps, self.loop = spec, fps, loop
        self.shared = shared
        self.event = asyncio.Event()
        self.lock = threading.Lock()
        self.closed = False
        self.received = self.selected_packets = self.replaced = self.expired = 0
        self.sent_packets = self.stale_h264_batches = 0
        self.wake_pending = False
        self.latest = None
        self.next_frame_at = 0.0
        self.h264 = H264Queue(max_age_sec=MAX_FRAME_AGE_NS / 1e9) if spec.topic_type == "h264" and not shared else None

    def offer(self, message):
        with self.lock:
            if self.closed:
                return
            self.received += 1
            if self.shared:
                return
            if self.h264 is not None:
                stamp = int(message.header.stamp.sec) * 1_000_000_000 + int(message.header.stamp.nanosec)
                if not self.h264.push(bytes(message.data), stamp):
                    return
            else:
                self.replaced += int(self.latest is not None)
                self.latest = (message, time.monotonic_ns())
            if self.wake_pending:
                return
            self.wake_pending = True
        try:
            self.loop.call_soon_threadsafe(self.event.set)
        except RuntimeError:
            self.close()

    def packets(self):
        with self.lock:
            self.wake_pending = False
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
        expired = time.monotonic_ns() - received > MAX_FRAME_AGE_NS
        if not selected or expired:
            self.expired += int(expired)
            return []
        packet = image_packet(message, self.spec.topic_type, self.spec.width, self.spec.height, received)
        if packet is None:
            return []
        self.selected_packets += 1
        return [packet]

    def discard_stale_h264(self, packet):
        if self.h264 is None or time.monotonic_ns() - HEADER.unpack_from(packet)[-1] <= MAX_FRAME_AGE_NS:
            return False
        with self.lock:
            self.stale_h264_batches += 1
            self.h264.reset()
        return True

    def close(self):
        with self.lock:
            self.closed = True
            self.latest = None
            if self.h264 is not None:
                self.h264.reset()


class NativePreviewHub:
    def __init__(self, cameras):
        self.specs = {
            camera.name: camera for camera in cameras
            if camera.topic_type in ("image", "compressed", "h264")
        }
        self._lock = threading.Lock()
        self._clients = {}
        self.paused = False

    def pause(self):
        with self._lock:
            self.paused = True
            clients = [c for group in self._clients.values() for c in group]
        for client in clients:
            client.close()
            client.loop.call_soon_threadsafe(client.event.set)

    def resume(self):
        with self._lock:
            self.paused = False

    def connect(self, name, fps, loop, *, shared=False):
        client = NativePreviewClient(self.specs[name], fps, loop, shared=shared)
        with self._lock:
            if self.paused:
                raise RuntimeError("Preview paused during capture")
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
        counters = ("received", "selected_packets", "sent_packets", "replaced", "expired", "stale_h264_batches")
        result = {"sessions": len(clients), **dict.fromkeys(counters, 0)}
        if any(c.shared for c in clients):
            result["shared_sessions"] = sum(c.shared for c in clients)
        for client in clients:
            with client.lock:
                for key in counters:
                    result[key] += getattr(client, key)
                if client.h264 is not None:
                    result["h264"] = client.h264.stats()
        return result
