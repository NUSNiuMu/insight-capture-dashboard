"""A common-map epoch must not splice old coordinates into the live display."""
from collections import deque
import json
import threading
from types import SimpleNamespace as S

import pytest
from insight_capture.core.models import PoseSpec
from insight_capture.runtime.mapping.stream import MappingStream
from insight_capture.runtime.payloads import PayloadBuilder
from insight_capture.runtime.ros.node import PoseBridgeNode

@pytest.fixture
def display():
    specs = [("head", "/insight9_sparse_map/pose", "head"),
             ("wrist", "/insight_global/insight3_a/pose", "left_hand"),
             ("local", "/camera/camera/vio_100hz", "right_hand")]
    node = S(pose_lock=threading.Lock(), trace_generation=1, pose_timeout_sec=2.,
             display_fps_limit=30., max_points=100, _playback_mode=False,
             _native_vio_pose_names=set(), _common_pose_stamp_floor={},
             get_clock=lambda:S(now=lambda:S(nanoseconds=1_000_000_000_000_000_000)),
             poses=[PoseSpec(n, topic, role, None, 1., (0.,0.,0.), (0.,0.,0.))
                    for n, topic, role in specs], local_vio_samples={})
    names = [p.name for p in node.poses]
    node.latest_pose_sample = dict.fromkeys(names)
    node.last_pose_received_time = dict.fromkeys(names, 0.)
    node.last_pose_source_stamp = {}
    node.raw_traces = {n: deque() for n in names}
    node.raw_trace_sequences = {n: deque() for n in names}
    node.trace_sequences = dict.fromkeys(names, 0)
    node._record_pose_sample = lambda *a: PoseBridgeNode._record_pose_sample(node, *a)
    node._stamp_to_ns = PoseBridgeNode._stamp_to_ns
    node.clear_traces = lambda: PoseBridgeNode.clear_traces(node)
    node.set_common_pose_epoch = lambda *a, **k: PoseBridgeNode.set_common_pose_epoch(node, *a, **k)
    node.stream = MappingStream(node)
    return node

def status(node, name="insight9", *, session="A", generation=1,
           reset=10, tracking_epoch=0, tracking_reset=0, revision=1):
    node.stream._status_callback(name)(S(data=json.dumps({
        "session_id": session, "generation": generation,
        "last_reset_wall_ns": reset * 1_000_000_000,
        "tracking_epoch": tracking_epoch,
        "last_pose_reset_wall_ns": tracking_reset * 1_000_000_000,
        "map_revision": revision, "state": "mapping", "localized": True})))

def point(node, name="head", *, stamp=11, x=12., live=True):
    callback = PoseBridgeNode._make_pose_callback(node, name, is_live=live)
    callback(S(header=S(stamp=S(sec=stamp, nanosec=0)),
               pose=S(position=S(x=x,y=0.,z=0.), orientation=S(x=0.,y=0.,z=0.,w=1.))))

def xs(node, name="head"):
    return [p[0] for p in node.raw_traces[name]]

@pytest.mark.parametrize("session,generation", [("A",2),("B",1)])
def test_new_world_clears_common_traces_but_preserves_independent_vio(display, session, generation):
    node = display
    status(node)
    for name in ["head", "wrist", "local"]:
        point(node, name)
    old_generation = node.trace_generation
    status(node, session=session, generation=generation, reset=20)
    assert node.trace_generation > old_generation
    assert not xs(node) and not xs(node,"wrist")
    assert xs(node,"local") == [12.]
    assert node.latest_pose_sample["head"] is None
    assert node.last_pose_received_time["head"] == 0.

@pytest.mark.parametrize("revision", [1,2,999])
def test_same_world_heartbeat_and_map_optimization_keep_path(display, revision):
    status(display)
    point(display)
    generation = display.trace_generation
    status(display, revision=revision)
    assert xs(display) == [12.]
    assert display.trace_generation == generation

def test_queued_old_pose_is_rejected_and_the_boundary_is_inclusive(display):
    status(display)
    point(display)
    status(display, generation=2, reset=20)
    point(display, stamp=19, x=99.)
    assert not xs(display)
    point(display, stamp=20, x=1.)
    point(display, stamp=21, x=2.)
    assert xs(display) == [1.,2.]

def test_old_generation_cannot_roll_back_the_active_world(display):
    status(display)
    status(display, generation=2, reset=20)
    point(display, stamp=21, x=1.)
    generation = display.trace_generation
    status(display, generation=1, reset=10)
    assert display.trace_generation == generation
    assert xs(display) == [1.]
    assert display.stream.snapshot()["statuses"]["insight9"]["generation"] == 2
    point(display, stamp=19, x=99.)
    assert xs(display) == [1.]

def test_retired_mapper_session_cannot_reappear_after_restart(display):
    status(display)
    status(display, session="B", reset=20)
    point(display, stamp=21, x=1.)
    generation = display.trace_generation
    status(display, session="A", generation=8, reset=10)
    assert display.trace_generation == generation
    assert display.stream.snapshot()["statuses"]["insight9"]["session_id"] == "B"
    assert xs(display) == [1.]

def test_wrist_reset_clears_only_its_global_path(display):
    status(display)
    status(display,"insight3_a")
    point(display)
    point(display,"wrist")
    point(display,"local")
    generation = display.trace_generation
    status(display,"insight3_a",tracking_epoch=1,tracking_reset=20)
    assert display.trace_generation > generation
    assert xs(display) == xs(display,"local") == [12.]
    assert not xs(display,"wrist")
    point(display,"wrist",stamp=19,x=99.)
    assert not xs(display,"wrist")
    point(display,"wrist",stamp=21,x=2.)
    assert xs(display,"wrist") == [2.]

def test_old_wrist_status_cannot_roll_back_its_epoch(display):
    status(display)
    status(display,"insight3_a")
    status(display,"insight3_a",tracking_epoch=1,tracking_reset=20)
    point(display,"wrist",stamp=21,x=1.)
    status(display,"insight3_a",tracking_epoch=0)
    point(display,"wrist",stamp=19,x=99.)
    assert xs(display,"wrist") == [1.]

def test_old_world_wrist_status_does_not_lower_a_new_world_boundary(display):
    status(display)
    status(display,"insight3_a")
    status(display,generation=2,reset=20)
    status(display,"insight3_a",generation=1,tracking_epoch=9,tracking_reset=10)
    point(display,"wrist",stamp=19,x=99.)
    assert not xs(display,"wrist")

def test_live_map_changes_do_not_erase_playback_or_reject_historical_bag_stamps(display):
    status(display)
    display._playback_mode = True
    point(display,stamp=1,x=7.,live=False)
    generation = display.trace_generation
    status(display,generation=2,reset=20)
    assert xs(display) == [7.]
    assert display.trace_generation == generation
    point(display,stamp=2,x=8.,live=False)
    assert xs(display) == [7.,8.]
    display._playback_mode = False
    display.clear_traces()
    point(display,stamp=19,x=99.)
    assert not xs(display)
    point(display,stamp=21,x=1.)
    assert xs(display) == [1.]

def test_independent_native_topic_display_does_not_inherit_map_timestamp_boundary(display):
    status(display)
    status(display,generation=2,reset=20)
    point(display,"local",stamp=1,x=7.)
    assert xs(display,"local") == [7.]
    assert "local" not in display._common_pose_stamp_floor

@pytest.mark.parametrize("patch", [{"session_id":None},{"session_id":[]},
    {"generation":True},{"generation":-1},{"generation":"2"},
    {"last_reset_wall_ns":None},{"last_reset_wall_ns":-1}])
def test_malformed_epoch_metadata_does_not_reset_the_live_trace(display, patch):
    status(display)
    point(display)
    generation = display.trace_generation
    value={"session_id":"A","generation":2,"last_reset_wall_ns":20_000_000_000,
           "state":"mapping"}
    value.update(patch)
    display.stream._status_callback("insight9")(S(data=json.dumps(value)))
    assert display.trace_generation == generation and xs(display) == [12.]

def test_world_change_emits_a_new_trace_snapshot_for_existing_viewer(display):
    status(display)
    point(display)
    before = PayloadBuilder(display).build_pose_payload()
    cursor={"generation":before["trace_generation"],"sequences":{
        p["name"]:p["trace_update"]["to_seq"] for p in before["poses"]}}
    status(display,generation=2,reset=20)
    point(display,stamp=21,x=1.)
    after = PayloadBuilder(display).build_pose_payload(cursor)
    assert after["trace_generation"] != before["trace_generation"]
    head=next(p for p in after["poses"] if p["name"]=="head")
    assert head["trace_update"]["mode"] == "snapshot"
    assert head["trace_update"]["points"] == [[1.,0.,0.]]


@pytest.mark.parametrize("name,pose_name", [("insight9","head"),("insight3_a","wrist")])
def test_clock_resynchronization_can_move_the_display_boundary_backwards(display, name, pose_name):
    status(display,reset=100)
    status(display,"insight3_a",reset=100)
    point(display,pose_name,stamp=101,x=12.)
    status(display,name,reset=100,tracking_epoch=1,tracking_reset=20)
    point(display,pose_name,stamp=21,x=1.)
    assert xs(display,pose_name) == [1.]
    assert display._common_pose_stamp_floor[pose_name] == 20_000_000_000
    status(display,name,reset=100,tracking_epoch=0)
    assert display._common_pose_stamp_floor[pose_name] == 20_000_000_000


@pytest.mark.parametrize("live,new_playback", [(True,True),(False,False)])
def test_callback_already_inflight_cannot_cross_a_playback_mode_switch(display, live, new_playback):
    display._playback_mode = not live
    display.get_logger = lambda:S(info=lambda *a:None)
    record = display._record_pose_sample
    def switch_then_record(*args):
        PoseBridgeNode.set_playback_mode(display,new_playback)
        record(*args)
    display._record_pose_sample = switch_then_record
    point(display,stamp=21,x=99.,live=live)
    assert not xs(display)
    assert display.latest_pose_sample["head"] is None


def test_queued_future_pose_from_before_clock_rollback_cannot_reenter(display):
    display.get_clock = lambda:S(now=lambda:S(nanoseconds=21_000_000_000))
    status(display, reset=100)
    status(display, tracking_epoch=1, tracking_reset=20, reset=100)
    point(display, stamp=21, x=1.)
    point(display, stamp=101, x=99.)
    assert xs(display) == [1.]
    assert display.latest_pose_sample['head'].position[0] == 1.


@pytest.mark.parametrize('stamp,accepted', [(23, True), (24, False)])
def test_common_future_tolerance_matches_source_clock_resynchronization_window(display, stamp, accepted):
    display.get_clock = lambda:S(now=lambda:S(nanoseconds=21_000_000_000))
    status(display, reset=20)
    point(display, stamp=stamp, x=2.)
    assert xs(display) == ([2.] if accepted else [])


def test_future_common_pose_rule_does_not_filter_native_clock_or_playback(display):
    display.get_clock = lambda:S(now=lambda:S(nanoseconds=21_000_000_000))
    status(display, reset=20)
    point(display, 'local', stamp=101, x=9.)
    assert xs(display, 'local') == [9.]
    display._playback_mode = True
    point(display, stamp=101, x=8., live=False)
    assert xs(display) == [8.]
