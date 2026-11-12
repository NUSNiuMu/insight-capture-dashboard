"""Failed history exposes recovery data without relabelling or rewriting Takes."""
import asyncio
import json
import io
from zipfile import ZipFile
from types import SimpleNamespace

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from insight_capture.api.routes.tasks import TaskRoutes
from insight_capture.runtime.take import SessionTakeStore
from insight_capture.runtime.tasks import CaptureTask, CaptureTaskCatalog


@pytest.fixture
def recovered(tmp_path):
    store = SessionTakeStore(tmp_path / 'results', task_catalog=CaptureTaskCatalog(
        [CaptureTask('qa', 'QA', 'QA', 'capture'), CaptureTask('other', 'Other', 'Other', 'capture')],
        default_task_id='qa'))
    take = store.reserve_take()
    original = tmp_path / take['bag_name']
    store.mark_recording(original)
    store.complete_current({'output_path': str(original), 'merge_state': 'error'})
    bag = original.with_name(original.name + '_recovered')
    bag.mkdir()
    data = b'\x89MCAP0\r\n' + b'fixture data' * 100
    (bag / 'data.mcap').write_bytes(data)
    (bag / 'metadata.yaml').write_text(yaml.safe_dump({'rosbag2_bagfile_information': {
        'storage_identifier': 'mcap', 'relative_file_paths': ['data.mcap'],
        'starting_time': {'nanoseconds_since_epoch': 110_000_000_000},
        'duration': {'nanoseconds': 7_000_000_000}, 'message_count': 50,
    }}))
    (bag / 'recording_manifest.json').write_text(json.dumps({
        'recovered': True, 'bag_name': bag.name, 'message_count': 50,
        'recovery_source': str(tmp_path / '_staging' / ('.' + original.name + '.original')),
    }))
    store.activate_task('other')
    return store, take, bag, data


def test_recovery_history_has_real_data_span_without_mutating_failed_take(recovered):
    store, take, bag, _ = recovered
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in store.results_root.rglob('*.json')}
    result = store.task_recordings('qa')['takes'][0]
    assert result['state'] == 'finalization_failed'
    assert result['recovery']['recording_timing']['duration_s'] == 7.
    assert result['recovery']['message_count'] == 50
    assert result['recovery']['scope'] == 'readable_messages_only'
    assert result['recovery']['bag_path'] == str(bag)
    assert store.task_status()['task']['task_id'] == 'other'
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before} == before
    assert store._take_stats([result])['valid_takes'] == 0


@pytest.mark.parametrize('fault', ['missing', 'bad_json', 'wrong_source', 'wrong_count', 'incomplete', 'not_recovered'])
def test_missing_or_unrelated_recovery_is_not_advertised(recovered, fault):
    store, _, bag, _ = recovered
    manifest = bag / 'recording_manifest.json'
    payload = json.loads(manifest.read_text())
    if fault == 'missing':
        manifest.unlink()
    elif fault == 'bad_json':
        manifest.write_text('[]')
    elif fault == 'incomplete':
        (bag / 'data.mcap').unlink()
    else:
        payload[{'wrong_source': 'recovery_source', 'wrong_count': 'message_count', 'not_recovered': 'recovered'}[fault]] = {
            'wrong_source': '/other/.other.original', 'wrong_count': 99, 'not_recovered': False}[fault]
        manifest.write_text(json.dumps(payload))
    assert 'recovery' not in store.task_recordings('qa')['takes'][0]


def test_download_is_resumable_and_limited_to_known_recovered_take_files(recovered):
    store, take, bag, data = recovered
    async def scenario():
        route = TaskRoutes(SimpleNamespace(take_store=store))
        app = web.Application()
        app.router.add_get('/api/tasks/{task_id}/recordings/{take_id}/recovered/{kind}', route._handle_recovered_file)
        async with TestClient(TestServer(app)) as client:
            base = f'/api/tasks/qa/recordings/{take["take_id"]}/recovered/'
            response = await client.get(base + 'data', headers={'Range': 'bytes=0-7'})
            assert response.status == 206
            assert await response.read() == data[:8]
            assert 'attachment' in response.headers['Content-Disposition']
            response = await client.head(base + 'data')
            assert response.status == 200
            assert int(response.headers['Content-Length']) == len(data)
            response = await client.get(base + 'bundle')
            assert response.status == 200
            assert bag.name + '.zip' in response.headers['Content-Disposition']
            with ZipFile(io.BytesIO(await response.read())) as archive:
                assert archive.testzip() is None
                assert sorted(archive.namelist()) == sorted(bag.name + '/' + name for name in (
                    'data.mcap', 'metadata.yaml', 'recording_manifest.json'))
                for name in archive.namelist():
                    assert archive.read(name) == (bag / name.split('/')[-1]).read_bytes()
            assert (await client.head(base + 'bundle')).status == 200
            for kind, name in [('metadata', 'metadata.yaml'), ('manifest', 'recording_manifest.json')]:
                response = await client.get(base + kind)
                assert await response.read() == (bag / name).read_bytes()
            for url in [base + 'private', base.replace('/qa/', '/unknown/') + 'data',
                        base.replace(f'/{take["take_id"]}/', '/999/') + 'data',
                        base.replace(f'/{take["take_id"]}/', '/-1/') + 'data']:
                assert (await client.get(url)).status == 404
    asyncio.run(scenario())
    assert store.task_status()['task']['task_id'] == 'other'


def test_bundle_keeps_large_reads_bounded_and_closes_after_cancel(tmp_path):
    from insight_capture.runtime.recovered_bundle import recovered_bundle
    path = tmp_path / 'large.mcap'
    path.write_bytes(bytes(range(256)) * 1024)
    chunks = recovered_bundle([path], chunk_size=4096)
    parts = list(chunks)
    assert max(map(len, parts)) <= 4096
    with ZipFile(io.BytesIO(b''.join(parts))) as archive:
        assert archive.read(tmp_path.name + '/' + path.name) == path.read_bytes()
    chunks = recovered_bundle([path], chunk_size=4096)
    next(chunks)
    next(chunks)
    chunks.close()
    assert chunks.gi_frame is None
