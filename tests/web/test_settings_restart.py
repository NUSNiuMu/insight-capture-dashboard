"""A stale maintenance confirmation must never interrupt capture."""

import asyncio
from types import SimpleNamespace

import pytest

from insight_capture.api.routes.recording import RecordingRoutes
from insight_capture.api.routes.settings import SettingsRoutes


def harness(state):
    context = SimpleNamespace(
        recording_manager=SimpleNamespace(status=lambda: dict(state)),
        node=SimpleNamespace(get_logger=lambda: SimpleNamespace(info=lambda _: None)),
    )
    recording = RecordingRoutes(context)
    return recording, SettingsRoutes(context, recording)


@pytest.mark.parametrize('state', [
    {'recording': True, 'recording_mode': 'capture'},
    {'recording': True, 'recording_mode': 'vio_calibration'},
    {'recording': False, 'merge_state': 'finalizing'},
    {'recording': False, 'merge_state': 'merging'},
    {'recording': False, 'distributed': {'phase': 'starting'}},
])
def test_stale_restart_confirmation_does_not_interrupt_capture(state):
    async def scenario():
        recording, settings = harness(state)
        response = await settings._handle_settings_restart(None)
        assert response.status == 409
        assert settings._restart_task is None
        assert not recording.busy
    asyncio.run(scenario())


def test_restart_during_preflight_never_schedules_exit():
    async def scenario():
        recording, settings = harness({'recording': False})
        async with recording._transition:
            response = await settings._handle_settings_restart(None)
            assert response.status == 409
            assert settings._restart_task is None
        assert not recording.busy
    asyncio.run(scenario())


def test_restart_keeps_new_starts_out_until_exit_and_deduplicates(monkeypatch):
    async def scenario():
        recording, settings = harness({'recording': False})
        entered, release = asyncio.Event(), asyncio.Event()
        exits = []
        async def delayed(_):
            entered.set()
            await release.wait()
        monkeypatch.setattr('insight_capture.api.routes.settings.asyncio.sleep', delayed)
        monkeypatch.setattr('insight_capture.api.routes.settings.os._exit', exits.append)
        assert (await settings._handle_settings_restart(None)).status == 200
        await entered.wait()
        try:
            assert recording.busy
            assert (await settings._handle_settings_restart(None)).status == 409
            with pytest.raises(RuntimeError, match='still running'):
                await recording._start_with_preflight(topics=None, bag_name=None)
            assert exits == []
        finally:
            release.set()
            await settings._restart_task
        assert exits == [0]
        assert not recording.busy
    asyncio.run(scenario())


def test_failed_status_read_releases_capture_admission():
    async def scenario():
        recording, settings = harness({'recording': False})
        def fail():
            raise OSError('status unavailable')
        settings.context.recording_manager.status = fail
        with pytest.raises(OSError):
            await settings._handle_settings_restart(None)
        assert settings._restart_task is None
        assert not recording.busy
    asyncio.run(scenario())
