"""Readable truncated data is verified before publishing a complete MCAP copy."""
from collections import Counter, deque
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from insight_capture.runtime.recording import recovery as module


def metadata(bag, count):
    (bag / 'metadata.yaml').write_text(yaml.safe_dump({'rosbag2_bagfile_information': {
        'storage_identifier': 'mcap', 'relative_file_paths': ['data.mcap'],
        'topics_with_message_count': [{'topic_metadata': {
            'name': '/imu', 'type': 'sensor_msgs/msg/Imu'}, 'message_count': count}]
    }}))


@pytest.fixture
def sample(tmp_path, monkeypatch):
    staging = tmp_path / '_staging' / 'qa_take_001'
    staging.mkdir(parents=True)
    (staging / 'data.mcap').write_bytes(b'original truncated bytes')
    (staging / module.RECORDING_TARGET_NAME).write_text('{"output_subdirectory":"qa"}')
    owner = SimpleNamespace(rosbag_root=tmp_path, _lock=threading.RLock(), _staging_dir=None,
                            _output_lines=deque(), _notify_recording_completed=Mock())
    recovery = module.RecordingRecovery(owner)
    rows = [('/imu', b'payload a', 42), ('/imu', b'payload b', 43)]
    monkeypatch.setattr(module, 'iter_messages', lambda path, topics: iter(rows))
    def run(command):
        if command[2] == 'reindex':
            metadata(Path(command[-1]), 2)
        else:
            config = yaml.safe_load(Path(command[-1]).read_text())
            generated = Path(config['output_bags'][0]['uri'])
            generated.mkdir()
            (generated / 'data.mcap').write_bytes(b'validated output fixture')
            metadata(generated, 2)
    monkeypatch.setattr(recovery, '_run', run)
    strict = Mock(return_value=(Counter({'/imu': 2}), {'/imu': 'sensor_msgs/msg/Imu'}))
    monkeypatch.setattr(recovery, '_validate', strict)
    return recovery, staging, rows, strict


def test_footer_is_required_only_after_conversion_and_original_is_preserved(sample):
    recovery, staging, _, strict = sample
    original = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in staging.iterdir()}
    recovery._recover_one_staging(staging)
    assert strict.call_count == 1 and strict.call_args.args[0].name == 'output'
    output = recovery.owner.rosbag_root / 'qa' / (staging.name + '_recovered')
    manifest = json.loads((output / module.MANIFEST_NAME).read_text())
    assert manifest['message_count'] == 2
    assert manifest['recovery_scope'] == 'readable_messages_only'
    assert 'unwritten' in manifest['recovery_warning']
    archive = staging.with_name('.' + staging.name + '.original')
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in archive.iterdir()} == original
    recovery.owner._notify_recording_completed.assert_called_once_with(output)


@pytest.mark.parametrize('change', ['payload', 'time', 'count'])
def test_changed_content_is_not_published_and_source_stays_intact(sample, monkeypatch, change):
    recovery, staging, rows, _ = sample
    original = (staging / 'data.mcap').read_bytes()
    def messages(path, topics):
        if Path(path).name != 'output':
            return iter(rows)
        altered = list(rows)
        altered[0] = ('/imu', b'changed' if change == 'payload' else b'payload a',
                      41 if change == 'time' else 42)
        return iter(altered[:1] if change == 'count' else altered)
    monkeypatch.setattr(module, 'iter_messages', messages)
    with pytest.raises(RuntimeError):
        recovery._recover_one_staging(staging)
    assert (staging / 'data.mcap').read_bytes() == original
    assert list((recovery.owner.rosbag_root / 'qa').iterdir()) == []
    recovery.owner._notify_recording_completed.assert_not_called()


def test_message_index_reordering_preserves_fingerprint(sample, monkeypatch):
    recovery, staging, rows, _ = sample
    monkeypatch.setattr(module, 'iter_messages', lambda path, topics:
                        iter(reversed(rows) if Path(path).name == 'output' else rows))
    recovery._recover_one_staging(staging)
    recovery.owner._notify_recording_completed.assert_called_once()


def test_no_readable_messages_keeps_original(sample, monkeypatch):
    recovery, staging, _, _ = sample
    monkeypatch.setattr(module, 'iter_messages', lambda *args: iter(()))
    with pytest.raises(RuntimeError, match='readable recording contents'):
        recovery._recover_one_staging(staging)
    assert (staging / 'data.mcap').read_bytes() == b'original truncated bytes'
