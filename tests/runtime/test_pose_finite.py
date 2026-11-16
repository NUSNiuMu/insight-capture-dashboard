"""Bad visualization poses must not poison shared JSON or raw input readiness."""
import json
from collections import deque
from types import SimpleNamespace
import threading
import pytest
from insight_capture.core.models import PoseSample,PoseSpec
from insight_capture.runtime.diagnostics import DiagnosticEvents
from insight_capture.runtime.payloads import PayloadBuilder
from insight_capture.runtime.ros.node import PoseBridgeNode
from insight_capture.runtime.ros import node as ros_node

def owner():
    logs=[]
    node=SimpleNamespace(
        pose_lock=threading.Lock(),trace_generation=1,pose_timeout_sec=2.,display_fps_limit=30.,max_points=100,
        poses=[PoseSpec('cam','/global/pose','head',None,1.,(0.,0.,0.),(0.,0.,0.))],
        _native_vio_pose_names=set(),_playback_mode=False,
        latest_pose_sample={'cam':None},last_pose_received_time={'cam':0.},last_pose_source_stamp={},
        raw_traces={'cam':deque()},raw_trace_sequences={'cam':deque()},trace_sequences={'cam':0},local_vio_samples={},
        events=DiagnosticEvents(SimpleNamespace(info=lambda text:logs.append(('info',text)),warning=lambda text:logs.append(('warning',text)),error=lambda text:logs.append(('error',text)))),
    )
    node._record_pose_sample=lambda *a:PoseBridgeNode._record_pose_sample(node,*a)
    node._record_local_vio=lambda *a:PoseBridgeNode._record_local_vio(node,*a)
    node._stamp_to_ns=PoseBridgeNode._stamp_to_ns
    return node,logs

def record(node,values=(1.,2.,3.,0.,0.,0.,1.),stamp=123):
    node._record_pose_sample('cam',PoseSample(tuple(values[:3]),tuple(values[3:])),stamp)

@pytest.mark.parametrize('component',range(7))
@pytest.mark.parametrize('bad',[float('nan'),float('inf'),-float('inf')])
def test_non_finite_component_never_replaces_finite_visualization(component,bad,monkeypatch):
    node,_=owner();monkeypatch.setattr(ros_node.time,'monotonic',lambda:100.);record(node)
    good=node.latest_pose_sample['cam'];values=[1.,2.,3.,0.,0.,0.,1.];values[component]=bad
    monkeypatch.setattr(ros_node.time,'monotonic',lambda:103.);record(node,values,stamp=999)
    assert node.latest_pose_sample['cam'] is good
    assert node.trace_sequences['cam']==1 and list(node.raw_trace_sequences['cam'])==[1]
    assert node.last_pose_received_time['cam']==100. and node.last_pose_source_stamp['cam']==123
    payload=PayloadBuilder(node).build_pose_payload();json.dumps(payload,allow_nan=False)
    assert payload['poses'][0]['visible'] is False

def test_repeated_invalid_samples_log_one_transition_then_recover():
    node,logs=owner();record(node)
    for _ in range(5):record(node,(float('nan'),2.,3.,0.,0.,0.,1.))
    record(node,(2.,3.,4.,0.,0.,0.,1.))
    events=node.events.snapshot()
    assert [e['state'] for e in events]==['non_finite_seen']
    assert sum(level=='warning' for level,_ in logs)==1
    assert node.trace_sequences['cam']==2
    json.dumps(PayloadBuilder(node).build_pose_payload(),allow_nan=False)

def test_invalid_native_pose_keeps_existing_packet_liveness_policy(monkeypatch):
    node,_=owner();node._native_vio_pose_names={'cam'};node.cameras=[SimpleNamespace(name='cam_rgb',device_name='cam')]
    node.camera_liveness_times={};node.local_vio_samples={'cam':{}}
    monkeypatch.setattr(ros_node.time,'monotonic',lambda:100.);record(node)
    local=node.local_vio_samples['cam']
    monkeypatch.setattr(ros_node.time,'monotonic',lambda:101.);record(node,(float('inf'),2.,3.,0.,0.,0.,1.))
    assert node.camera_liveness_times=={'cam':101.,'cam_rgb':101.}
    assert node.local_vio_samples['cam'] is local
    assert node.trace_sequences['cam']==1

def test_ros_callback_keeps_original_message_and_recovers_on_next_good_sample():
    node,_=owner();callback=PoseBridgeNode._make_pose_callback(node,'cam')
    msg=SimpleNamespace(header=SimpleNamespace(stamp=SimpleNamespace(sec=1,nanosec=0)),pose=SimpleNamespace(position=SimpleNamespace(x=float('nan'),y=2.,z=3.),orientation=SimpleNamespace(x=0.,y=0.,z=0.,w=1.)))
    callback(msg);assert node.trace_sequences['cam']==0
    assert msg.pose.position.x!=msg.pose.position.x
    msg.pose.position.x=1.;callback(msg);assert node.trace_sequences['cam']==1
    json.dumps(PayloadBuilder(node).build_pose_payload(),allow_nan=False)

def test_finite_zero_and_large_values_are_not_new_recording_or_geometry_gates():
    node,_=owner();record(node,(0.,1e6,-1e6,0.,0.,0.,0.))
    assert node.trace_sequences['cam']==1
    assert node.latest_pose_sample['cam'].orientation_xyzw==(0.,0.,0.,0.)
