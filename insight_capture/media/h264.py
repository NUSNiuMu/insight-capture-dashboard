"""Bounded Annex B access-unit queues that recover at a complete IDR."""

from collections import deque
import re
import threading


def nal_units(data: bytes):
    """Yield Annex B NAL units, including their start codes."""
    starts = list(re.finditer(b"\x00\x00(?:\x00)?\x01", data))
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(data)
        if start.end() < end:
            yield data[start.end()] & 31, data[start.start():end]


class H264Gate:
    """Cache parameter sets and suppress dependent frames after discontinuities."""

    def __init__(self):
        self.sps = b""
        self.pps = b""
        self.waiting = True

    def reset(self):
        self.waiting = True

    def accept(self, data: bytes):
        units = list(nal_units(data))
        kinds = {kind for kind, _ in units}
        for kind, unit in units:
            if kind == 7:
                if self.sps and self.sps != unit:
                    self.reset()
                self.sps = unit
            elif kind == 8:
                if self.pps and self.pps != unit:
                    self.reset()
                self.pps = unit
        if not kinds.intersection({1, 5}):
            return None
        if 5 in kinds and self.sps and self.pps:
            self.waiting = False
            return (b"" if 7 in kinds else self.sps) + (b"" if 8 in kinds else self.pps) + data
        return None if self.waiting else data


class H264Queue:
    """Keep short contiguous GOP segments; overflow waits for the next IDR."""

    def __init__(self, max_frames=8, max_bytes=8 * 1024 * 1024, fps=30):
        self._lock = threading.Lock()
        self._frames = deque()
        self._bytes = 0
        self._gate = H264Gate()
        self._stamp = None
        self.max_frames = max_frames
        self.max_bytes = max_bytes
        self.max_gap_ns = int(1.5e9 / fps)
        self.dropped = 0

    def reset(self):
        with self._lock:
            self._frames.clear()
            self._bytes = 0
            self._stamp = None
            self._gate = H264Gate()

    def push(self, data, stamp_ns=None):
        with self._lock:
            discontinuity = (
                stamp_ns is not None and self._stamp is not None
                and not 0 < stamp_ns - self._stamp <= self.max_gap_ns
            )
            if stamp_ns is not None:
                self._stamp = stamp_ns
            if discontinuity or len(self._frames) >= self.max_frames or self._bytes + len(data) > self.max_bytes:
                self.dropped += len(self._frames)
                self._frames.clear()
                self._bytes = 0
                self._gate.reset()
            data = self._gate.accept(data)
            if data is None or len(data) > self.max_bytes:
                self.dropped += 1
                if data is not None:
                    self._gate.reset()
                return False
            self._frames.append(data)
            self._bytes += len(data)
            return True

    def drain(self):
        with self._lock:
            result = list(self._frames)
            self._frames.clear()
            self._bytes = 0
            return result
