"""Bounded Annex B access-unit queues that recover at a complete IDR."""

from collections import deque
import hashlib
import os
import re
import threading
import time


def nal_units(data: bytes):
    """Yield Annex B NAL units, including their start codes."""
    starts = list(re.finditer(b"\x00\x00(?:\x00)?\x01", data))
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(data)
        if start.end() < end:
            yield data[start.end()] & 31, data[start.start():end]


def frame_fingerprint(data: bytes):
    """Match a VCL NAL across parsers that may rewrite Annex B start codes."""
    for kind, unit in nal_units(data):
        if kind in (1, 5):
            nal = unit[4:] if unit.startswith(b"\x00\x00\x00\x01") else unit[3:]
            return hashlib.sha256(nal.rstrip(b"\x00")[:64]).hexdigest()[:32]
    return ""


class _Bits:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def read(self, count):
        if self.pos + count > len(self.data) * 8:
            raise ValueError("Truncated H.264 header")
        value = 0
        for _ in range(count):
            value = (value << 1) | ((self.data[self.pos // 8] >> (7 - self.pos % 8)) & 1)
            self.pos += 1
        return value

    def ue(self):
        zeros = 0
        while not self.read(1):
            zeros += 1
            if zeros > 31:
                raise ValueError("Invalid H.264 Exp-Golomb code")
        return (1 << zeros) - 1 + self.read(zeros)


class H264ReferenceSequence:
    """Recognize contiguous progressive baseline/main I/P reference pictures.

    Other profiles or slice structures return unknown so callers keep their
    conservative timestamp policy. A capture gap need not be an encoded-frame loss.
    """

    def __init__(self):
        self.sps = {}
        self.pps = {}
        self.previous = None
        self.modulus = 0

    def observe(self, data):
        try:
            for kind, unit in nal_units(data):
                nal = unit[4:] if unit.startswith(b"\x00\x00\x00\x01") else unit[3:]
                rbsp = re.sub(b"\x00\x00\x03", b"\x00\x00", nal[:256])
                bits = _Bits(rbsp[1:])
                if kind == 7:
                    profile = bits.read(8)
                    bits.read(16)
                    identity = bits.ue()
                    if profile not in (66, 77, 88):
                        self.sps.pop(identity, None)
                        continue
                    width = bits.ue() + 4
                    poc = bits.ue()
                    if poc == 0:
                        bits.ue()
                    elif poc == 1:
                        bits.read(1)
                        bits.ue()
                        bits.ue()
                        cycle = bits.ue()
                        if cycle > 255:
                            raise ValueError("Invalid picture order count cycle")
                        for _ in range(cycle):
                            bits.ue()
                    elif poc != 2:
                        raise ValueError("Unsupported picture order count")
                    bits.ue()
                    bits.read(1)
                    bits.ue()
                    bits.ue()
                    progressive = bits.read(1)
                    if 4 <= width <= 16 and progressive:
                        if self.sps.get(identity) != width:
                            self.previous = None
                        self.sps[identity] = width
                    else:
                        self.sps.pop(identity, None)
                elif kind == 8:
                    identity, sps = bits.ue(), bits.ue()
                    self.pps[identity] = sps
                elif kind in (1, 5):
                    first_mb, slice_type, pps = bits.ue(), bits.ue() % 5, bits.ue()
                    width = self.sps.get(self.pps.get(pps))
                    if first_mb != 0 or slice_type not in (0, 2) or not (nal[0] & 96) or width is None:
                        self.previous = None
                        return None
                    number = bits.read(width)
                    self.modulus = 1 << width
                    previous = self.previous
                    self.previous = number
                    if kind == 5:
                        return True
                    return None if previous is None else number == (previous + 1) % self.modulus
        except (ValueError, IndexError):
            self.previous = None
        return None


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
        self._sequence = H264ReferenceSequence()
        self.fps = fps
        self.max_frames = max_frames
        self.max_bytes = max_bytes
        self.max_gap_ns = int(1.5e9 / fps)
        self.dropped = 0
        self.received = 0
        self.timestamp_gaps = 0
        self.timestamp_rollbacks = 0
        self.overflows = 0
        self.waiting_drops = 0
        self.reference_gaps = 0
        self._trace_enabled = os.environ.get("INSIGHT_LATENCY_TRACE") == "1"
        self._trace = deque(maxlen=120)

    def stats(self):
        with self._lock:
            return {
                "received": self.received, "dropped": self.dropped,
                "timestamp_gaps": self.timestamp_gaps,
                "timestamp_rollbacks": self.timestamp_rollbacks,
                "overflows": self.overflows, "waiting_drops": self.waiting_drops,
                "reference_gaps": self.reference_gaps,
                "queued_frames": len(self._frames), "queued_bytes": self._bytes,
                "waiting_for_idr": self._gate.waiting,
                "frame_trace": list(self._trace),
            }

    def reset(self):
        with self._lock:
            self._frames.clear()
            self._bytes = 0
            self._stamp = None
            self._gate = H264Gate()
            self._sequence = H264ReferenceSequence()

    def push(self, data, stamp_ns=None):
        received_ns = time.monotonic_ns()
        with self._lock:
            if self._trace_enabled:
                self._trace.append({"key": frame_fingerprint(data), "received_ns": received_ns})
            self.received += 1
            if stamp_ns is not None and self._stamp is not None:
                self.timestamp_gaps += int(stamp_ns - self._stamp > self.max_gap_ns)
                self.timestamp_rollbacks += int(stamp_ns <= self._stamp)
            contiguous = self._sequence.observe(data)
            self.reference_gaps += int(contiguous is False)
            gap_ns = None if stamp_ns is None or self._stamp is None else stamp_ns - self._stamp
            # Never infer continuity across a complete frame_num wrap or a long outage.
            capture_gap = contiguous is True and gap_ns is not None and 0 < gap_ns < min(8, self._sequence.modulus - 1) * 1e9 / self.fps
            discontinuity = contiguous is False or (
                stamp_ns is not None and self._stamp is not None
                and not 0 < stamp_ns - self._stamp <= self.max_gap_ns
                and not capture_gap
            )
            if stamp_ns is not None:
                self._stamp = stamp_ns
            overflow = len(self._frames) >= self.max_frames or self._bytes + len(data) > self.max_bytes
            self.overflows += int(overflow)
            if discontinuity or overflow:
                self.dropped += len(self._frames)
                self._frames.clear()
                self._bytes = 0
                self._gate.reset()
            data = self._gate.accept(data)
            if data is None or len(data) > self.max_bytes:
                self.waiting_drops += int(data is None)
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
