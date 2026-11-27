"""Local NVMM preview sockets published by the stereo decoder."""

import fcntl
import grp
import os
from pathlib import Path
import re
import stat
import time

ROOT = Path("/dev/shm/insight-egohalo-nvmm")
_SOURCE = re.compile(r"[a-z0-9_]{1,64}")


def shared_decode_sources(config):
    sources = config.get("egohalo_rectification", {}).get("shared_preview_sources", [])
    if not isinstance(sources, list) or not all(isinstance(name, str) and _SOURCE.fullmatch(name) for name in sources):
        raise ValueError("shared_preview_sources must contain camera names")
    return frozenset(sources)


def preview_socket(name):
    if not _SOURCE.fullmatch(name):
        raise ValueError("Invalid shared preview camera name")
    return ROOT / f"{name}.sock"


def ready_preview_socket(name):
    path = preview_socket(name)
    try:
        root = ROOT.lstat()
        socket = path.lstat()
    except OSError:
        return None
    if (stat.S_ISDIR(root.st_mode) and root.st_uid == os.geteuid() and not root.st_mode & 0o022
            and stat.S_ISSOCK(socket.st_mode) and socket.st_uid == root.st_uid and socket.st_mode & 0o020):
        return path
    return None


def _demand_fd(path):
    return os.open(str(path) + ".viewers", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)


def hold_preview_demand(path):
    fd = _demand_fd(path)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise
    return fd


class SharedDecodeSocket:
    def __init__(self, name, width, height):
        self.path = preview_socket(name)
        scale = min(1.0, 720.0 / max(width, height))
        self.width, self.height = (max(2, int(value * scale) // 2 * 2) for value in (width, height))
        ROOT.mkdir(mode=0o755, exist_ok=True)
        info = ROOT.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise PermissionError("Invalid shared decode socket directory")
        self.group = grp.getgrnam("video").gr_gid
        self.fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.path.unlink(missing_ok=True)
            self.demand = _demand_fd(self.path)
            self._demand_check = 0.0
            self._viewing = False
        except BaseException:
            os.close(self.fd)
            raise

    def has_viewers(self):
        now = time.monotonic()
        if now < self._demand_check:
            return self._viewing
        self._demand_check = now + .1
        try:
            fcntl.flock(self.demand, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._viewing = True
        else:
            fcntl.flock(self.demand, fcntl.LOCK_UN)
            self._viewing = False
        return self._viewing

    def allow_client(self):
        os.chown(self.path, -1, self.group, follow_symlinks=False)
        self.path.chmod(0o660)

    def close(self):
        try:
            self.path.unlink(missing_ok=True)
        finally:
            os.close(self.demand)
            os.close(self.fd)
