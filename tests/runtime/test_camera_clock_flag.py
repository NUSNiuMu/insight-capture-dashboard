"""An incomplete sync flag makes camera publishers change timestamp domains."""
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def clock(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[2] / 'deploy/camera-clock/clock.py'
    spec = importlib.util.spec_from_file_location('qa_camera_clock', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'SYNC_FLAG', tmp_path / 'is_time_sync')
    return module


def test_flag_transition_never_exposes_partial_data(clock, monkeypatch):
    clock.SYNC_FLAG.write_bytes(b'1\n')
    replace = clock.os.replace
    observed = []
    def inspect_before_replace(source, target):
        # A reader scheduled while the writer prepares the new value still
        # reads the entire previous value, including a real loss of sync.
        observed.append((Path(target).read_bytes(), Path(source).read_bytes()))
        replace(source, target)
    monkeypatch.setattr(clock.os, 'replace', inspect_before_replace)
    clock.publish_sync_state(False)
    assert clock.SYNC_FLAG.read_bytes() == b'0\n'
    clock.publish_sync_state(True)
    assert clock.SYNC_FLAG.read_bytes() == b'1\n'
    assert observed == [(b'1\n', b'0\n'), (b'0\n', b'1\n')]


def test_unchanged_flag_does_not_rewrite_storage(clock):
    clock.publish_sync_state(True)
    before = clock.SYNC_FLAG.stat()
    clock.publish_sync_state(True)
    after = clock.SYNC_FLAG.stat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)
    assert after.st_mode & 0o777 == 0o644


def test_failed_replace_preserves_old_value_and_removes_temporary(clock, monkeypatch):
    clock.publish_sync_state(True)
    def fail(*args):
        raise OSError('simulated storage failure')
    monkeypatch.setattr(clock.os, 'replace', fail)
    with pytest.raises(OSError):
        clock.publish_sync_state(False)
    assert clock.SYNC_FLAG.read_bytes() == b'1\n'
    assert list(clock.SYNC_FLAG.parent.iterdir()) == [clock.SYNC_FLAG]


def test_invalid_existing_flag_is_replaced(clock):
    clock.SYNC_FLAG.write_bytes(b'\xff')
    clock.publish_sync_state(False)
    assert clock.SYNC_FLAG.read_bytes() == b'0\n'


def test_new_daemon_failure_cannot_leave_previous_ready_flag(clock, tmp_path, monkeypatch):
    clock.publish_sync_state(True)
    monkeypatch.setattr(clock, 'RUN', tmp_path / 'run')
    def fail(*args, **kwargs):
        raise OSError('simulated daemon launch failure')
    monkeypatch.setattr(clock.subprocess, 'Popen', fail)
    with pytest.raises(OSError, match='daemon launch failure'):
        clock.main()
    assert clock.SYNC_FLAG.read_bytes() == b'0\n'


def test_service_cleanup_clears_flag_without_starting_clock(clock, monkeypatch):
    clock.publish_sync_state(True)
    monkeypatch.setattr(clock, 'main', lambda: pytest.fail('cleanup must not start a daemon'))
    clock.cli(['--clear-sync-state'])
    assert clock.SYNC_FLAG.read_bytes() == b'0\n'
    previous = clock.SYNC_FLAG.stat()
    clock.cli(['--clear-sync-state'])
    assert clock.SYNC_FLAG.stat().st_mtime_ns == previous.st_mtime_ns
