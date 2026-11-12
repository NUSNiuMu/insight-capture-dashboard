"""Coordinate capture admission with the offline installer on the shared config mount."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path


LOCK_PATH = Path(__file__).resolve().parents[2] / "config" / ".capture-upgrade.lock"


@contextmanager
def recording_start_lock():
    # Read-only descriptors can flock on the Linux deployment filesystem. This
    # lets the host installer and container use one inode despite different UIDs.
    # Never unlink it: replacing the inode would split the two lock domains.
    descriptor = os.open(LOCK_PATH, os.O_RDONLY | os.O_CREAT | os.O_CLOEXEC, 0o644)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("系统正在升级，请等待升级完成后再开始录制。") from exc
        yield
    finally:
        os.close(descriptor)
