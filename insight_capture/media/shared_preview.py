"""One latest packed NV12 frame for each enabled local preview."""

from hashlib import sha256
import fcntl
import mmap
import os
from pathlib import Path
import stat
import struct
import tempfile
import time
from types import SimpleNamespace

_HEADER = struct.Struct("<QQII")
_MAX_FRAME_AGE_NS = 150_000_000


def shared_preview_topics(settings):
    topics = settings.get("shared_preview_topics", [])
    if not isinstance(topics, list) or not all(isinstance(topic, str) for topic in topics):
        raise ValueError("recording_h264.shared_preview_topics must be a list of image topics")
    if len(set(topics)) != len(topics) or set(topics) - set(settings.get("input_topics", [])):
        raise ValueError("Shared preview topics must be distinct recording encoder inputs")
    return set(topics)


def preview_path(domain, topic):
    name = sha256(f"{domain}:{topic}".encode()).hexdigest()[:24]
    return Path("/dev/shm") / f"insight-preview-{name}.nv12"


class PreviewWriter:
    def __init__(self, path, width, height):
        self.path = path
        self.width = width
        self.height = height
        self.fd, staging = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        self.mapping = None
        try:
            self.identity = os.fstat(self.fd).st_ino
            os.posix_fallocate(self.fd, 0, _HEADER.size + width * height * 3 // 2)
            self.mapping = mmap.mmap(self.fd, 0)
            os.replace(staging, path)
        except BaseException:
            if self.mapping is not None:
                self.mapping.close()
            os.close(self.fd)
            os.unlink(staging)
            raise

    def offer(self, data, source_ns, received_ns):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        try:
            self.mapping[_HEADER.size:] = data
            _HEADER.pack_into(self.mapping, 0, received_ns, source_ns, self.width, self.height)
        finally:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        return True

    def close(self):
        self.mapping.close()
        os.close(self.fd)
        try:
            if self.path.stat().st_ino == self.identity:
                self.path.unlink()
        except FileNotFoundError:
            pass


class PreviewReader:
    def __init__(self, path):
        self.path = path
        self.stream = None
        self.mapping = None
        self.identity = None
        self.last_received_ns = 0
        self.next_check_ns = 0

    def close(self):
        if self.stream is not None:
            self.mapping.close()
            self.stream.close()
            self.stream = self.mapping = None
            self.identity = None

    def _reopen(self):
        try:
            information = self.path.lstat()
        except FileNotFoundError:
            self.close()
            return
        if information.st_ino == self.identity:
            return
        self.close()
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        candidate = os.fdopen(fd, "rb")
        try:
            information = os.fstat(fd)
            if (not stat.S_ISREG(information.st_mode) or information.st_uid != os.geteuid()
                    or stat.S_IMODE(information.st_mode) != 0o600
                    or not _HEADER.size < information.st_size <= _HEADER.size + 64 * 1024 * 1024):
                raise ValueError("Invalid shared preview buffer")
            self.mapping = mmap.mmap(fd, 0, access=mmap.ACCESS_READ)
        except BaseException:
            candidate.close()
            raise
        self.stream = candidate
        self.identity = information.st_ino

    def take(self, now_ns=None):
        now_ns = time.monotonic_ns() if now_ns is None else now_ns
        if now_ns >= self.next_check_ns and now_ns - self.last_received_ns > _MAX_FRAME_AGE_NS:
            self.next_check_ns = now_ns + 200_000_000
            self._reopen()
        if self.stream is None:
            return None
        fd = self.stream.fileno()
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        try:
            received, stamp, width, height = _HEADER.unpack_from(self.mapping)
            if (received <= self.last_received_ns or not 0 <= now_ns - received <= _MAX_FRAME_AGE_NS
                    or stamp <= 0 or width <= 0 or height <= 0 or (width | height) & 1
                    or len(self.mapping) != _HEADER.size + width * height * 3 // 2):
                return None
            payload = self.mapping[_HEADER.size:]
            self.last_received_ns = received
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
        sec, nanosec = divmod(stamp, 1_000_000_000)
        return SimpleNamespace(width=width, height=height, step=width, encoding="nv12", data=payload,
                               header=SimpleNamespace(stamp=SimpleNamespace(sec=sec, nanosec=nanosec)))
