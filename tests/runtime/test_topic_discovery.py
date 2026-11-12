"""Admission uses the owned live graph without cold ROS subprocess discovery."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading
from unittest.mock import Mock

import pytest

from insight_capture.runtime import bootstrap
from insight_capture.runtime.recording.manager import RecordingManager


TOPIC = '/head/camera/imu'
CONFIG = {'cameras': [{'name': 'head', 'namespace': 'head', 'enabled': True}],
          'recording_excluded_topics': ['/head/camera/inactive']}


def manager(tmp_path, provider):
    return RecordingManager(CONFIG, 20, tmp_path, 1024, [TOPIC, '/tf_static'],
                            topic_provider=provider)


def test_published_snapshot_keeps_profile_filter_and_default_selection(tmp_path):
    provider = Mock(return_value=[TOPIC, '/tf_static', '/head/camera/inactive',
                                 '/other/camera/imu', '/insight_global/head/status'])
    subject = manager(tmp_path, provider)
    result = subject.current_topic_catalog()
    assert result['topics'] == [TOPIC, '/tf_static', '/insight_global/head/status']
    assert result['default_selected_topics'] == [TOPIC, '/tf_static']
    assert subject.current_topic_catalog(refresh=False) == result
    provider.assert_called_once_with()


def test_graph_failure_invalidates_cached_admission_until_recovery(tmp_path):
    provider = Mock(side_effect=[[TOPIC], RuntimeError('input node unavailable'), []])
    subject = manager(tmp_path, provider)
    assert TOPIC in subject.current_topic_catalog()['topics']
    with pytest.raises(RuntimeError, match='input node unavailable'):
        subject.refresh_topic_catalog(force=True)
    with pytest.raises(RuntimeError, match='input node unavailable'):
        subject.current_topic_catalog(refresh=False)
    assert subject.topic_catalog['topics'] == []
    assert subject.current_topic_catalog()['topics'] == []
    assert 'error' not in subject.current_topic_catalog(refresh=False)


def test_unwired_graph_reports_failure_without_starting_ros_cli(tmp_path, monkeypatch):
    cli = Mock(side_effect=AssertionError('must not launch a CLI discovery participant'))
    monkeypatch.setattr('subprocess.run', cli)
    subject = manager(tmp_path, None)
    with pytest.raises(RuntimeError, match='provider is unavailable'):
        subject.current_topic_catalog()
    cli.assert_not_called()


def test_disappearing_publisher_expires_from_cache_and_can_return(tmp_path, monkeypatch):
    now = [100.]
    monkeypatch.setattr('insight_capture.runtime.recording.manager.time.monotonic', lambda: now[0])
    provider = Mock(side_effect=[[TOPIC], [], [TOPIC]])
    subject = manager(tmp_path, provider)
    assert subject.current_topic_catalog()['topics'] == [TOPIC]
    now[0] = 100.5
    assert subject.current_topic_catalog()['topics'] == [TOPIC]
    now[0] = 101.1
    assert subject.current_topic_catalog()['topics'] == []
    now[0] = 101.5
    assert subject.current_topic_catalog()['topics'] == []
    assert provider.call_count == 2
    assert subject.refresh_topic_catalog(force=True)['topics'] == [TOPIC]


def test_slow_rebind_snapshot_uses_completion_time_and_does_not_lock_recorder(tmp_path, monkeypatch):
    now = [100.]
    monkeypatch.setattr('insight_capture.runtime.recording.manager.time.monotonic', lambda: now[0])
    def provider():
        assert not subject.status()['recording']
        now[0] += 5
        return [TOPIC]
    source = Mock(side_effect=provider)
    subject = manager(tmp_path, source)
    subject.current_topic_catalog()
    now[0] += .5
    subject.current_topic_catalog()
    source.assert_called_once_with()


def test_concurrent_clients_share_one_snapshot(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def provider():
        entered.set()
        assert release.wait(2)
        return [TOPIC]
    source = Mock(side_effect=provider)
    subject = manager(tmp_path, source)
    with ThreadPoolExecutor(max_workers=4) as pool:
        first = pool.submit(subject.current_topic_catalog)
        assert entered.wait(2)
        rest = [pool.submit(subject.current_topic_catalog) for _ in range(3)]
        assert not subject.status()['recording']
        release.set()
        assert all(future.result(timeout=2)['topics'] == [TOPIC] for future in [first, *rest])
    source.assert_called_once_with()


def test_bootstrap_binds_renewable_camera_inputs_not_initial_node(tmp_path, monkeypatch):
    provider = Mock(return_value=[TOPIC])
    inputs = SimpleNamespace(published_topics=provider)
    node = SimpleNamespace(camera_inputs=inputs, _preview_manager=object(), cameras=[],
                           start_image_recording=Mock(), stop_image_recording=Mock(),
                           drain_image_recording=Mock())
    settings = SimpleNamespace(runtime_config={}, raw_config=CONFIG, ros_domain_id=20,
                               rosbag_root=tmp_path, default_record_topics=[TOPIC],
                               storage_status={}, configured_rosbag_root=tmp_path,
                               project_root=tmp_path, storage_browse_roots=[tmp_path])
    subject = bootstrap.build_recording_manager(settings, node)
    assert subject.current_topic_catalog()['topics'] == [TOPIC]
    provider.assert_called_once_with()
