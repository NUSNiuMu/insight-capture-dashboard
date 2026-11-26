"""Ordinary catalog, lifecycle and persistence regressions from the code review."""
import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from insight_capture.core.bag import is_mcap_bag
from insight_capture.postprocess.bags.catalog import BagLibrary
from insight_capture.runtime.take import SessionTakeStore
from insight_capture.runtime.anomaly import ActiveQcMonitor
from insight_capture.api.routes.recording import RecordingRoutes


@pytest.mark.parametrize('relative', [None, 42, [{}]])
def test_catalog_skips_invalid_metadata(tmp_path, relative):
    for name, files in [('valid', ['data.mcap']), ('invalid', relative)]:
        bag = tmp_path / name
        bag.mkdir()
        (bag / 'data.mcap').touch()
        (bag / 'metadata.yaml').write_text(yaml.safe_dump({'rosbag2_bagfile_information': {
            'storage_identifier': 'mcap', 'relative_file_paths': files}}))
    library = BagLibrary(lambda: [tmp_path], lambda: tmp_path)
    assert [item.path.name for item in library.locations()] == ['valid']
    assert not is_mcap_bag(tmp_path / 'invalid')


def test_takes_sort_numerically(tmp_path):
    for number in (9999, 10000):
        (tmp_path / f'take_{number:04d}.json').write_text(json.dumps({'take_id': number}))
    (tmp_path / 'take_notes.json').write_text('{}')
    assert [item['take_id'] for item in SessionTakeStore._list_takes_from(
        tmp_path, include_timing=False)] == [10000, 9999]


def test_anomaly_persistence_retries_start_and_resolution():
    writes = []
    fail = [True]
    def persist(event):
        if fail[0]:
            fail[0] = False
            raise OSError('temporary write failure')
        writes.append(event['event'])
    qc = ActiveQcMonitor(None, None, SimpleNamespace(add_anomaly=persist))
    qc._pending_since['dummy'] = 0
    with patch('insight_capture.runtime.anomaly.time.monotonic', return_value=10):
        with pytest.raises(OSError):
            qc._observe('dummy', True, 'fault')
        qc._observe('dummy', True, 'fault')
        fail[0] = True
        with pytest.raises(OSError):
            qc._observe('dummy', False, 'fault')
        qc._observe('dummy', False, 'fault')
    assert writes == ['started', 'resolved']


def test_raw_capture_does_not_require_global_localization(tmp_path):
    events = []
    manager = SimpleNamespace(is_recording=lambda: True, rosbag_root=tmp_path,
        status=lambda: {'recording_mode': 'capture', 'require_global_pose': False,
                        'output_path': 'dummy'})
    node = SimpleNamespace(cameras=[], build_mapping_payload=lambda: {'statuses': {}})
    qc = ActiveQcMonitor(node, manager, SimpleNamespace(add_anomaly=events.append),
                         {'minimum_free_gb': 0})
    with patch('insight_capture.runtime.anomaly.time.monotonic', return_value=10):
        qc.poll()
    with patch('insight_capture.runtime.anomaly.time.monotonic', return_value=20):
        qc.poll()
    assert not any(event['code'].startswith('localization:') for event in events)


def test_cancelled_stop_keeps_transition_until_finalized():
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        completed = []
        def stop():
            entered.set()
            assert release.wait(3)
            return {'recording': False}
        context = SimpleNamespace(recording_manager=SimpleNamespace(
            status=lambda: {'recording_mode': 'capture'}, stop=stop),
            take_store=SimpleNamespace(complete_current=completed.append))
        route = RecordingRoutes(context)
        operation = asyncio.create_task(route.stop_recording())
        while not entered.is_set():
            await asyncio.sleep(.001)
        operation.cancel()
        await asyncio.sleep(.01)
        try:
            assert route.busy
            assert not operation.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert completed == [{'recording': False}]
        assert not route.busy
    asyncio.run(scenario())


def test_missing_static_topic_fails_integrity(tmp_path):
    from insight_capture.postprocess.bags.integrity import _add_missing_selected_topics
    (tmp_path / 'recording_manifest.json').write_text(json.dumps({
        'selected_topics': ['/camera/tf_static']}))
    topics = []
    _add_missing_selected_topics(tmp_path, topics)
    assert topics[0]['msgs'] == 0
    assert topics[0]['ok'] is False


@pytest.mark.parametrize('payload', [None, [], {'cameras': [None]}])
def test_invalid_playback_cache_rebuilds(tmp_path, payload):
    from insight_capture.postprocess.bags.synchronization import PreparedPlaybackManager, SCHEMA_VERSION, _cache_key
    if isinstance(payload, dict):
        payload = {**payload, "schema_version": SCHEMA_VERSION, "source_signature": [],
                   "configuration": {}, "cache_key": _cache_key([], {})}
    manager = PreparedPlaybackManager.__new__(PreparedPlaybackManager)
    manager._review_dir = lambda name: tmp_path
    manager._bag_resolver = None
    (tmp_path / 'manifest.json').write_text(json.dumps(payload))
    assert manager._cache_valid('dummy', [], {}) is False


def test_completed_merge_status_without_stop_timestamp():
    from insight_capture.runtime.recording.distributed import DistributedRecordingManager
    manager = DistributedRecordingManager.__new__(DistributedRecordingManager)
    manager.local = SimpleNamespace(status=lambda: {})
    manager._bypass = False
    manager._lock = threading.RLock()
    manager._session = {'phase': 'done', 'output_path': '/unused',
                        'finished_at_epoch_s': 30, 'merge_started_at_epoch_s': 20}
    manager._clock = {}
    manager._last_error = None
    assert manager.status()['merge_timings']['total_sec'] == 10


def test_stop_retains_writer_that_survives_signals(tmp_path, monkeypatch):
    import subprocess
    from insight_capture.runtime.recording import manager as module
    manager = module.RecordingManager({}, 0, tmp_path, 1024, ['/dummy'])
    def wait(timeout):
        raise subprocess.TimeoutExpired('dummy writer', timeout)
    process = SimpleNamespace(pid=999999, returncode=None, poll=lambda: None, wait=wait)
    manager.processes = {'single': process}
    manager._staging_dir = tmp_path
    monkeypatch.setattr(module.os, 'killpg', lambda *args: None)
    monkeypatch.setattr(module.os, 'getpgid', lambda pid: pid)
    monkeypatch.setattr(module, 'capture_network_snapshot', lambda: {})
    status = manager._stop(timeout_sec=.01)
    assert status['recording'] and status['merge_state'] == 'error'
    assert manager.processes == {'single': process}
    with pytest.raises(RuntimeError, match='already running'):
        manager._start()
    with pytest.raises(RuntimeError):
        manager.delete_saved_recordings(lambda: pytest.fail('delete while writer is alive'))


def test_start_failure_shuts_down_spawned_writer(tmp_path, monkeypatch):
    from insight_capture.runtime.recording import manager as module
    manager = module.RecordingManager({}, 0, tmp_path, 1024, ['/dummy'])
    manager._recorder_ready = SimpleNamespace(clear=lambda: None, wait=lambda timeout: True)
    process = SimpleNamespace(pid=999999, returncode=None)
    process.poll = lambda: process.returncode
    def terminate(*args):
        process.returncode = 0
    process.wait = lambda timeout: process.returncode
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *args, **kwargs: process)
    monkeypatch.setattr(module.pty, 'openpty', lambda: (23456, 23457))
    monkeypatch.setattr(module.os, 'close', lambda fd: None)
    monkeypatch.setattr(module.os, 'getpgid', lambda pid: pid)
    monkeypatch.setattr(module.os, 'killpg', terminate)
    monkeypatch.setattr(module.threading, 'Thread', lambda **kwargs: SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(Path, 'write_text', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('marker write failed')))
    with pytest.raises(OSError, match='marker write failed'):
        manager._start(bag_name='dummy', output_subdirectory='task_dummy')
    assert process.poll() == 0
    assert not manager.processes


def test_dynamic_network_refresh_keeps_exclusions(tmp_path, monkeypatch):
    from insight_capture.runtime.ros import network
    selected = []
    def addresses(value):
        selected.append(value.decode().split(','))
        return 0
    library = SimpleNamespace(insight_set_capture_addresses=addresses)
    monkeypatch.delenv('FASTRTPS_DEFAULT_PROFILES_FILE', raising=False)
    monkeypatch.delenv('FASTDDS_DEFAULT_PROFILES_FILE', raising=False)
    monkeypatch.setattr(network, '_dynamic_profile', None)
    monkeypatch.setattr(network, '_dynamic_excluded', frozenset())
    monkeypatch.setattr(network, '_dynamic_namespaces', None)
    monkeypatch.setattr(network, '_transport_library', library)
    monkeypatch.setattr(network, 'camera_links', lambda: [SimpleNamespace(address='192.0.2.1'), SimpleNamespace(address='192.0.2.2')])
    profile = network.configure_ros_network({'ros_transport': {
        'camera_usb_only': True, 'exclude_host_addresses': ['192.0.2.1']}})
    try:
        network.refresh_ros_network()
        assert selected == [['127.0.0.1', '192.0.2.2']] * 2
    finally:
        profile.unlink(missing_ok=True)
        monkeypatch.delenv('FASTRTPS_DEFAULT_PROFILES_FILE', raising=False)


def test_same_basename_scores_keep_separate_results(tmp_path, monkeypatch):
    from insight_capture.services import scoring
    bags = {key: tmp_path / key / 'same' for key in ('a', 'b')}
    for bag in bags.values():
        bag.mkdir(parents=True)
    manager = scoring.ScoringManager(tmp_path, tmp_path / 'results', bag_resolver=bags.__getitem__)
    monkeypatch.setattr(scoring.threading, 'Thread', lambda **kwargs: SimpleNamespace(start=lambda: None))
    def score(command, **kwargs):
        Path(command[-1]).write_text(json.dumps({'source': command[3]}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(scoring.subprocess, 'run', score)
    for key in bags:
        assert manager.run(key, '/dummy/cov')
        manager._worker(manager._current_job)
        assert manager.status['bag_name'] == key
    summaries = list((tmp_path / 'results/scores').glob('bag-*.json'))
    summaries = [path for path in summaries if '__' not in path.name]
    assert len(summaries) == 2
    assert {json.loads(path.read_text())['cameras'][0]['source'] for path in summaries} == {str(path) for path in bags.values()}


@pytest.mark.parametrize('stamps,traces,median,sigma', [
    ([1., 2.], [float('nan'), float('nan')], -3., .4),
    ([1., float('inf')], [.01, .02], -3., .4),
    ([1.], [.01, .02], -3., .4),
    ([], [], -3., .4),
    ([1., 2.], [.01, .02], -3., 0),
    ([1., 2.], [.01, .02], float('nan'), .4),
])
def test_scoring_rejects_unusable_measurements(stamps, traces, median, sigma):
    from insight_capture.postprocess.quality.trajectory_score import compute_stats
    with pytest.raises(ValueError):
        compute_stats(stamps, traces, median, sigma)


def test_finite_trajectory_keeps_a_finite_score():
    import math
    from insight_capture.postprocess.quality.trajectory_score import compute_stats
    result = compute_stats([1., 2.], [.001, .002], -3., .4)
    assert math.isfinite(result['score']) and 0 <= result['score'] <= 100



@pytest.mark.parametrize('shutdown', ['ExternalShutdownException', 'ShutdownException'])
def test_expected_ros_shutdown_is_orderly(shutdown, monkeypatch):
    from insight_capture.runtime import app
    def spin():
        raise getattr(app, shutdown)()
    monkeypatch.setattr(app.os, '_exit', lambda code: pytest.fail('normal shutdown must not exit the process'))
    node = SimpleNamespace(get_logger=lambda: SimpleNamespace(fatal=lambda text: pytest.fail(text)))
    app._run_executor(SimpleNamespace(spin=spin), node)


def test_same_basename_catalog_badges_use_stable_ids(tmp_path):
    from insight_capture.postprocess.bags.catalog import list_rosbag_locations
    for directory in ('a', 'b'):
        bag = tmp_path / directory / 'same'
        bag.mkdir(parents=True)
        (bag / 'data.mcap').touch()
        (bag / 'metadata.yaml').write_text(yaml.safe_dump({'rosbag2_bagfile_information': {
            'storage_identifier': 'mcap', 'relative_file_paths': ['data.mcap']}}))
    library = BagLibrary(lambda: [tmp_path], lambda: tmp_path)
    locations = sorted(library.locations(), key=lambda item: str(item.path))
    results = tmp_path / '_outputs'
    for category in ('scores', 'integrity'):
        (results / category).mkdir(parents=True)
        # An old ambiguous basename report must not badge both recordings.
        (results / category / 'same.json').write_text('{"ok": true}')
        (results / category / (locations[0].bag_id + '.json')).write_text('{"ok": true}')
    rows = list_rosbag_locations(locations, results)
    assert [(row['scored'], row['integrity']) for row in rows] == [(True, True), (False, None)]


def test_raw_capture_still_reports_device_vio_outage(tmp_path, monkeypatch):
    now = 10.
    events = []
    camera = SimpleNamespace(name='head_stream', device_name='insight9_a', label='Head')
    node = SimpleNamespace(cameras=[camera], poses=[SimpleNamespace(name='insight9_a')],
        camera_input_lock=threading.RLock(), camera_input_times={'head_stream': [now]},
        camera_liveness_times={'head_stream': now}, camera_vio_status={})
    manager = SimpleNamespace(is_recording=lambda: True, rosbag_root=tmp_path,
        status=lambda: {'recording_mode': 'capture', 'require_global_pose': False, 'output_path': 'raw'})
    qc = ActiveQcMonitor(node, manager, SimpleNamespace(add_anomaly=events.append), {'minimum_free_gb': 0})
    monkeypatch.setattr('insight_capture.runtime.anomaly.time.monotonic', lambda: now)
    for now in (10., 14., 20.):
        node.camera_input_times['head_stream'].append(now)
        qc.poll()
    assert [event['code'] for event in events] == ['vio_stale:insight9_a']
