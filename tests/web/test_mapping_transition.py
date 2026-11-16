"""Calibration admission must share capture transitions without waiting for RPC replies."""
import asyncio
import contextlib
import threading
import time
from types import SimpleNamespace
import pytest
from insight_capture.api.routes.mapping import MappingRoutes
from insight_capture.api.routes.recording import RecordingRoutes
from insight_capture.runtime.take import SessionTakeStore
from insight_capture.runtime.tasks import CaptureTask,CaptureTaskCatalog


def _fixture(root, mode=None, merge_state="idle", phase=None):
    now=time.monotonic();calls=[]
    node=SimpleNamespace(cameras=[SimpleNamespace(name="insight9_a", label="Head")],
        poses=[SimpleNamespace(name="insight9_a", teleop_role="head")],
        camera_stale_timeout_sec=2, camera_input_lock=threading.Lock(),
        camera_input_times={"insight9_a":[now]},camera_liveness_times={"insight9_a":now})
    def reset(*, on_dispatched=None):
        calls.append("reset")
        if on_dispatched:on_dispatched()
        return {"ok":True}
    node.reset_mapping=reset
    state={"recording":mode is not None,"recording_mode":mode or "capture","merge_state":merge_state,
        "output_path":str(root/"current"),"distributed":{"phase":phase or "idle"}}
    def stop(**kwargs):
        calls.append("stop");state["recording"]=False
        return dict(state)
    manager=SimpleNamespace(status=lambda:dict(state),stop=stop,
        start=lambda **kw:{"recording":True,"output_path":str(root/kw["bag_name"])})
    store=SessionTakeStore(root/"results",task_catalog=CaptureTaskCatalog([
        CaptureTask("task_a","A","A","capture")],default_task_id="task_a"))
    context=SimpleNamespace(node=node,recording_manager=manager,take_store=store,
        capture_preflight=SimpleNamespace(evaluate=lambda *a,**kw:{"ok":True}))
    # Match the production app's construction order, mapping before recording.
    mapping=MappingRoutes(context);recording=RecordingRoutes(context)
    return context,mapping,recording,calls,state


def test_reset_is_rejected_during_actual_start_preflight(tmp_path):
    async def scenario():
        context,mapping,recording,calls,state=_fixture(tmp_path)
        entered,release=threading.Event(),threading.Event()
        def evaluate(*a,**kw):
            entered.set();assert release.wait(3);return {"ok":True}
        context.capture_preflight.evaluate=evaluate
        start=asyncio.create_task(recording._start_with_preflight(topics=None,bag_name=None,expected_task_id="task_a"))
        assert await asyncio.to_thread(entered.wait,2)
        try:
            assert recording.busy
            response=await mapping._handle_reset(None)
            assert response.status==409 and calls==[], (response.status,calls)
        finally:
            release.set();await start
    asyncio.run(scenario())


def test_reset_is_rejected_during_actual_stop_transition(tmp_path):
    async def scenario():
        context,mapping,recording,calls,state=_fixture(tmp_path,mode="capture")
        entered,release=threading.Event(),threading.Event()
        def stop(**kwargs):
            entered.set();assert release.wait(3)
            state["recording"]=False;return dict(state)
        context.recording_manager.stop=stop
        stopping=asyncio.create_task(recording.stop_recording())
        assert await asyncio.to_thread(entered.wait,2)
        try:
            assert recording.busy
            response=await mapping._handle_reset(None)
            assert response.status==409 and calls==[]
        finally:
            release.set();await stopping
    asyncio.run(scenario())


def test_normal_capture_still_allows_mapping_reset(tmp_path):
    async def scenario():
        context,mapping,recording,calls,state=_fixture(tmp_path,mode="capture")
        assert (await mapping._handle_reset(None)).status==200
        assert calls==["reset"] and not recording.busy
    asyncio.run(scenario())


@pytest.mark.parametrize("mode,merge,phase", [("vio_calibration","idle",None),
    (None,"finalizing",None),(None,"merging",None),(None,"idle","starting")])
def test_disallowed_existing_phases_do_not_dispatch_reset(tmp_path,mode,merge,phase):
    async def scenario():
        context,mapping,recording,calls,state=_fixture(tmp_path,mode,merge,phase)
        assert (await mapping._handle_reset(None)).status==409
        assert not calls and not recording.busy
    asyncio.run(scenario())


def test_start_cannot_enter_before_reset_is_dispatched(tmp_path):
    async def scenario():
        context,mapping,recording,calls,state=_fixture(tmp_path)
        entered,dispatch,done=threading.Event(),threading.Event(),threading.Event()
        def reset(*,on_dispatched=None):
            try:
                entered.set();assert dispatch.wait(3)
                calls.append("reset")
                if on_dispatched:on_dispatched()
                return {"ok":True}
            finally:done.set()
        context.node.reset_mapping=reset
        resetting=asyncio.create_task(mapping._handle_reset(None))
        assert await asyncio.to_thread(entered.wait,2)
        try:
            with pytest.raises(RuntimeError,match="operation"):
                await recording._start_with_preflight(topics=None,bag_name=None)
        finally:
            dispatch.set();await resetting;assert await asyncio.to_thread(done.wait,1)
    asyncio.run(scenario())


def test_stop_does_not_wait_for_mapping_response(tmp_path, monkeypatch):
    from concurrent.futures import Future
    from insight_capture.runtime.mapping.stream import MappingStream
    from insight_capture.runtime.ros.node import PoseBridgeNode
    import insight_capture.runtime.mapping.stream as module
    monkeypatch.setattr(module,"Empty",SimpleNamespace(Request=object))
    async def scenario():
        context,mapping,recording,calls,state=_fixture(tmp_path,mode="capture")
        dispatched=threading.Event();reply=Future()
        context.node.clear_traces=lambda:calls.append("clear")
        stream=MappingStream(context.node)
        def send(request):
            calls.append("reset");dispatched.set();return reply
        stream._reset_client=SimpleNamespace(service_is_ready=lambda:True,call_async=send)
        context.node._mapping_stream=stream
        context.node.reset_mapping=lambda **kwargs:PoseBridgeNode.reset_mapping(context.node,**kwargs)
        resetting=asyncio.create_task(mapping._handle_reset(None))
        assert await asyncio.to_thread(dispatched.wait,2)
        try:
            for _ in range(100):
                if not recording.busy:break
                await asyncio.sleep(.001)
            response=await recording.stop_recording()
            assert response.status==200 and calls==["reset","stop"]
            assert not resetting.done(),"Stop must remain usable before calibration replies"
        finally:
            reply.set_result(object());await resetting
        assert calls==["reset","stop","clear"]
    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_count", [1,2])
def test_cancelled_reset_keeps_admission_until_dispatch(tmp_path,cancel_count):
    async def scenario():
        context,mapping,recording,calls,state=_fixture(tmp_path)
        entered,dispatch,reply,done=[threading.Event() for _ in range(4)]
        def reset(*,on_dispatched=None):
            try:
                entered.set();assert dispatch.wait(3)
                if on_dispatched:on_dispatched()
                assert reply.wait(3)
                return {"ok":True}
            finally:done.set()
        context.node.reset_mapping=reset
        resetting=asyncio.create_task(mapping._handle_reset(None))
        assert await asyncio.to_thread(entered.wait,2)
        try:
            assert recording.busy
            for _ in range(cancel_count):
                resetting.cancel();await asyncio.sleep(0)
                assert recording.busy and not resetting.done()
            dispatch.set()
            with pytest.raises(asyncio.CancelledError):await resetting
            assert not recording.busy
        finally:
            dispatch.set();reply.set()
            with contextlib.suppress(asyncio.CancelledError):await resetting
            assert await asyncio.to_thread(done.wait,1)
    asyncio.run(scenario())


def test_dispatch_failure_releases_shared_admission(tmp_path):
    async def scenario():
        context,mapping,recording,calls,state=_fixture(tmp_path)
        def reset(*,on_dispatched=None):raise RuntimeError("dispatch failed")
        context.node.reset_mapping=reset
        with pytest.raises(RuntimeError,match="dispatch failed"):await mapping._handle_reset(None)
        assert not recording.busy
    asyncio.run(scenario())
