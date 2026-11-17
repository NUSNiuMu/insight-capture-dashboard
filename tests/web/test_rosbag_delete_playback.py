"""Deleting a recording must coordinate with its prepared playback."""
import asyncio
import shutil
import threading
from types import SimpleNamespace

import pytest

from insight_capture.api.routes.playback import PlaybackRoutes
from insight_capture.api.routes.recording import RecordingRoutes
from insight_capture.postprocess.bags import BagLibrary
from insight_capture.postprocess.bags.synchronization import PreparedPlaybackManager


def fixture(root, phase="playing"):
    def bag(name):
        path = root / name
        path.mkdir()
        (path / "capture.mcap").write_bytes(b"isolated fixture")
        (path / "metadata.yaml").write_text(
            "rosbag2_bagfile_information:\n  storage_identifier: mcap\n  message_count: 1\n"
        )
        return path
    selected, other = bag("selected"), bag("other")
    library = BagLibrary(lambda: [root], lambda: root)
    reference = library.reference_for_path(selected)
    prepared = PreparedPlaybackManager(root, root / "cache")
    prepared._state = "ready"
    prepared._bag_name = reference
    if phase == "playing":
        prepared.activate(reference)
    else:
        prepared._state = phase
    context = SimpleNamespace(
        bag_library=library, prepared_playback_manager=prepared,
        recording_manager=SimpleNamespace(status=lambda: {
            "recording": False, "merge_state": "idle", "output_path": None,
        }),
        node=SimpleNamespace(set_playback_mode=lambda _: None),
    )
    return context, RecordingRoutes(context), selected, other, reference


@pytest.mark.parametrize("phase", ["preparing", "playing"])
def test_in_use_bag_is_preserved(tmp_path, phase):
    async def scenario():
        context, routes, bag, _, reference = fixture(tmp_path, phase)
        response = await routes._handle_rosbag_delete(
            SimpleNamespace(match_info={"bag_name": reference})
        )
        assert response.status == 409
        assert bag.is_dir() and (bag / "capture.mcap").read_bytes() == b"isolated fixture"
        assert context.prepared_playback_manager.status()["state"] == phase
    asyncio.run(scenario())


def test_deleting_other_bag_does_not_interrupt_playback(tmp_path):
    async def scenario():
        context, routes, selected, other, _ = fixture(tmp_path)
        reference = context.bag_library.reference_for_path(other)
        response = await routes._handle_rosbag_delete(
            SimpleNamespace(match_info={"bag_name": reference})
        )
        assert response.status == 200 and not other.exists()
        assert selected.is_dir()
        assert context.prepared_playback_manager.status()["state"] == "playing"
    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["idle", "ready"])
def test_unused_bag_can_be_deleted_without_stale_ready_state(tmp_path, phase):
    async def scenario():
        context, routes, selected, _, reference = fixture(tmp_path, phase)
        response = await routes._handle_rosbag_delete(
            SimpleNamespace(match_info={"bag_name": reference})
        )
        assert response.status == 200 and not selected.exists()
        assert context.prepared_playback_manager.status()["state"] == "idle"
        with pytest.raises(RuntimeError):
            context.prepared_playback_manager.activate(reference)
    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_request", [False, True])
def test_activation_cannot_race_an_admitted_delete(tmp_path, monkeypatch, cancel_request):
    async def scenario():
        context, routes, selected, _, reference = fixture(tmp_path, "ready")
        entered, release = threading.Event(), threading.Event()
        original = shutil.rmtree

        def remove(path):
            entered.set()
            assert release.wait(3)
            original(path)

        monkeypatch.setattr("insight_capture.api.routes.recording.shutil.rmtree", remove)
        deletion = asyncio.create_task(routes._handle_rosbag_delete(
            SimpleNamespace(match_info={"bag_name": reference})
        ))
        assert await asyncio.to_thread(entered.wait, 2)
        if cancel_request:
            deletion.cancel()
            await asyncio.sleep(0)

        class Request:
            async def json(self):
                return {"bag_name": reference}

        try:
            with pytest.raises(RuntimeError):
                await PlaybackRoutes(context)._handle_playback_activate(Request())
        finally:
            release.set()
            if cancel_request:
                with pytest.raises(asyncio.CancelledError):
                    await deletion
            else:
                assert (await deletion).status == 200
        assert not selected.exists()
        assert context.prepared_playback_manager.status()["state"] == "idle"
    asyncio.run(scenario())
