"""Late desktop playback requests must not gate a live capture."""
import asyncio
import threading
from types import SimpleNamespace
import pytest
from insight_capture.api.routes.playback import PlaybackRoutes
from insight_capture.api.routes.recording import RecordingRoutes
from insight_capture.postprocess.bags.synchronization import PreparedPlaybackManager
from insight_capture.runtime.ros.node import PoseBridgeNode

class Request:
    async def json(self):return {"bag_name":"old_take"}

def fixture(root):
    calls=[];state={"recording":False,"merge_state":"idle","recording_mode":"capture"}
    def start(**kwargs):
        calls.append("start");state["recording"]=True
        return {**state,"output_path":str(root/"new_take")}
    manager=SimpleNamespace(status=lambda:dict(state),start=start)
    prepared=PreparedPlaybackManager(root,root/"cache")
    # A previous, successfully prepared take; no media decoding or ROS I/O.
    prepared._state="ready";prepared._bag_name="old_take"
    node=SimpleNamespace(_playback_mode=False,pose_lock=threading.Lock(),get_logger=lambda:SimpleNamespace(info=lambda _:None),clear_traces=lambda:calls.append("clear"))
    node.set_playback_mode=lambda enabled:PoseBridgeNode.set_playback_mode(node,enabled)
    context=SimpleNamespace(recording_manager=manager,prepared_playback_manager=prepared,node=node,
        capture_preflight=SimpleNamespace(evaluate=lambda *a,**kw:{"ok":True}))
    return context,PlaybackRoutes(context),RecordingRoutes(context),calls,state

async def response(call):
    try:return (await call).status
    except RuntimeError:return 409


def test_late_activate_cannot_gate_a_new_recording(tmp_path):
    async def scenario():
        context,playback,recording,calls,state=fixture(tmp_path)
        started=await recording._start_with_preflight(topics=None,bag_name="new_take",track_take=False)
        assert started.status==200 and state["recording"]
        assert await response(playback._handle_playback_activate(Request()))==409
        assert not context.node._playback_mode
        assert context.prepared_playback_manager.status()["state"]=="ready"
        assert calls==["start"]
    asyncio.run(scenario())


def test_late_activate_is_rejected_during_actual_start_preflight(tmp_path):
    async def scenario():
        context,playback,recording,calls,state=fixture(tmp_path)
        entered,release=threading.Event(),threading.Event()
        def evaluate(*a,**kw):entered.set();assert release.wait(3);return {"ok":True}
        context.capture_preflight.evaluate=evaluate
        start=asyncio.create_task(recording._start_with_preflight(topics=None,bag_name="new_take",track_take=False))
        assert await asyncio.to_thread(entered.wait,2)
        try:
            assert await response(playback._handle_playback_activate(Request()))==409
            assert not context.node._playback_mode
        finally:release.set();await start
    asyncio.run(scenario())


def test_capture_cannot_start_while_prepared_media_is_playing(tmp_path):
    async def scenario():
        context,playback,recording,calls,state=fixture(tmp_path)
        assert (await playback._handle_playback_activate(Request())).status==200
        assert await response(recording._start_with_preflight(topics=None,bag_name="new_take",track_take=False))==409
        assert calls==[] and not state["recording"]
        assert context.node._playback_mode
    asyncio.run(scenario())


def test_duplicate_live_request_does_not_clear_live_capture_traces(tmp_path):
    async def scenario():
        context,playback,recording,calls,state=fixture(tmp_path)
        await recording._start_with_preflight(topics=None,bag_name="new_take",track_take=False)
        assert (await playback._handle_playback_stop(None)).status==200
        assert calls==["start"],"A stale Live request cleared the current live trajectory"
        assert not context.node._playback_mode and state["recording"]
    asyncio.run(scenario())


def test_normal_playback_live_capture_cycle_preserves_idempotent_live(tmp_path):
    async def scenario():
        context,playback,recording,calls,state=fixture(tmp_path)
        assert (await playback._handle_playback_activate(Request())).status==200
        assert context.node._playback_mode
        assert (await playback._handle_playback_stop(None)).status==200
        assert not context.node._playback_mode and calls==["clear"]
        assert (await playback._handle_playback_stop(None)).status==200
        assert calls==["clear"]
        assert (await recording._start_with_preflight(topics=None,bag_name="new_take",track_take=False)).status==200
        assert calls==["clear","start"] and state["recording"]
    asyncio.run(scenario())


@pytest.mark.parametrize("operation",["prepare","stop"])
def test_playback_requests_cannot_enter_capture_preflight(tmp_path,operation):
    async def scenario():
        context,playback,recording,calls,state=fixture(tmp_path)
        entered,release=threading.Event(),threading.Event()
        def evaluate(*a,**kw):entered.set();assert release.wait(3);return {"ok":True}
        context.capture_preflight.evaluate=evaluate
        start=asyncio.create_task(recording._start_with_preflight(topics=None,bag_name="new_take",track_take=False))
        assert await asyncio.to_thread(entered.wait,2)
        try:
            call=playback._handle_playback_start(Request()) if operation=="prepare" else playback._handle_playback_stop(None)
            assert await response(call)==409
            assert context.prepared_playback_manager.status()["state"]=="ready"
            assert calls==[] and not context.node._playback_mode
        finally:release.set();await start
    asyncio.run(scenario())


@pytest.mark.parametrize("phase",["finalizing","merging","starting"])
def test_preparing_or_activating_playback_cannot_interrupt_saving(tmp_path,phase):
    async def scenario():
        context,playback,recording,calls,state=fixture(tmp_path)
        if phase=="starting":state["distributed"]={"phase":phase}
        else:state["merge_state"]=phase
        for call in [playback._handle_playback_start,playback._handle_playback_activate]:
            assert await response(call(Request()))==409
        assert context.prepared_playback_manager.status()["state"]=="ready"
        assert calls==[] and not context.node._playback_mode
        assert not recording.busy
    asyncio.run(scenario())


def test_failed_activation_does_not_gate_capture_or_retain_transition(tmp_path):
    async def scenario():
        context,playback,recording,calls,state=fixture(tmp_path)
        context.prepared_playback_manager.stop()
        assert await response(playback._handle_playback_activate(Request()))==409
        assert not context.node._playback_mode and not recording.busy
        assert (await recording._start_with_preflight(topics=None,bag_name="new_take",track_take=False)).status==200
        assert calls==["start"]
    asyncio.run(scenario())
