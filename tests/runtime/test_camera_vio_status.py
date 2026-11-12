from collections import deque
import threading
from types import SimpleNamespace

import pytest

from insight_capture.runtime.camera_health import evaluate_calibration_cameras
from insight_capture.runtime.ros import node as ros_node


def owner():
    node = SimpleNamespace(
        cameras=[SimpleNamespace(name=n, device_name='head', namespace='camera_head', label=n)
                 for n in ['head_rgb', 'head_ir']],
        poses=[SimpleNamespace(name='head')],
        camera_input_lock=threading.Lock(),
        camera_input_times={n: deque([100.0]) for n in ['head_rgb', 'head_ir']},
        camera_liveness_times={n: 0.0 for n in ['head_rgb', 'head_ir']},
        camera_vio_status={}, _playback_mode=False,
    )
    return node


@pytest.mark.parametrize('state', ['NOT_INITED', 'NO_CALIBRATION', 'TRACKING_LOST',
                                 'RESTARTING', 'STOPPED', 'DATA_LOST', 'TRACKING', 'FUTURE_STATE'])
def test_native_status_explains_missing_poses_without_satisfying_readiness(monkeypatch, state):
    node = owner()
    _, missing = evaluate_calibration_cameras(node, 2., now=100.)
    callback = ros_node.PoseBridgeNode._make_camera_status_callback(node, ['head'])
    monkeypatch.setattr(ros_node.time, 'monotonic', lambda: 100.)
    callback(SimpleNamespace(data=state))
    health, after = evaluate_calibration_cameras(node, 2., now=100.5)
    assert health['head']['vio_status'] == state
    assert health['head']['vio_status_age_sec'] == .5
    assert health['head']['vio_status_fresh']
    assert not health['head']['ready']
    assert after[0]['missing'] == missing[0]['missing'] == ['vio']
    assert node.camera_liveness_times == {'head_rgb': 0., 'head_ir': 0.}


def test_status_expiry_cannot_present_an_old_cause_or_add_a_recording_gate():
    node = owner()
    node.camera_vio_status['head'] = ('NO_CALIBRATION', 98.)
    for name in node.camera_liveness_times:
        node.camera_liveness_times[name] = 100.
    fresh, _ = evaluate_calibration_cameras(node, 2., now=100.)
    assert fresh['head']['vio_status_fresh']
    assert fresh['head']['ready']
    stale, missing = evaluate_calibration_cameras(node, 2., now=100.01)
    assert stale['head']['vio_status'] is None
    assert not stale['head']['vio_status_fresh']
    assert stale['head']['ready'] and not missing
    del node.camera_vio_status
    legacy, missing = evaluate_calibration_cameras(node, 2., now=100.01)
    assert legacy['head']['ready'] and not missing
    assert legacy['head']['vio_status_age_sec'] is None


def test_status_subscription_groups_streams_by_device_and_ignores_playback(monkeypatch):
    node = owner()
    node.ros_callback_group = object()
    node.subscription_event_callbacks = None
    subscriptions = []
    node.camera_inputs = SimpleNamespace(create_subscription=lambda *a, **k: subscriptions.append(a))
    node._make_camera_status_callback = lambda devices: ros_node.PoseBridgeNode._make_camera_status_callback(node, devices)
    monkeypatch.setattr(ros_node, 'make_image_qos', lambda depth: depth)
    monkeypatch.setattr(ros_node.time, 'monotonic', lambda: 100.)
    ros_node.PoseBridgeNode._create_camera_status_subscriptions(node)
    assert len(subscriptions) == 1
    assert subscriptions[0][1] == '/camera_head/camera/vio_status'
    callback = subscriptions[0][2]
    callback(SimpleNamespace(data='NOT_INITED'))
    assert node.camera_vio_status == {'head': ('NOT_INITED', 100.)}
    node._playback_mode = True
    callback(SimpleNamespace(data='TRACKING_STATIC'))
    assert node.camera_vio_status['head'][0] == 'NOT_INITED'
