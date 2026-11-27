from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest


def test_raw_input_has_its_own_executor_across_participant_refresh(monkeypatch):
    pytest.importorskip("rclpy")
    from insight_capture.runtime import ros_inputs

    nodes, executors, threads = [], [], []

    def node(name, *, context):
        value = Mock()
        value.get_name.return_value = name
        nodes.append((value, context))
        return value

    def executor(**kwargs):
        value = Mock()
        executors.append(value)
        return value

    def thread(**kwargs):
        value = Mock()
        threads.append((value, kwargs))
        return value

    monkeypatch.setattr(ros_inputs, "Node", node)
    monkeypatch.setattr(ros_inputs, "SingleThreadedExecutor", executor)
    monkeypatch.setattr(ros_inputs, "Context", Mock())
    monkeypatch.setattr(ros_inputs, "refresh_ros_network", Mock())
    monkeypatch.setattr(ros_inputs, "camera_transport_links", frozenset)
    monkeypatch.setattr(ros_inputs.threading, "Thread", thread)
    owner = SimpleNamespace(
        context=SimpleNamespace(get_domain_id=lambda: 20),
        get_name=lambda: "dashboard",
        events=Mock(),
    )
    inputs = ros_inputs.CameraInputs(owner)
    raw_args = (object(), "/head/h264", Mock(), Mock())
    wrist_args = (object(), "/wrist/image", Mock(), Mock())
    group = object()
    fallback_args = (object(), "/wrist/shared", Mock(), Mock())
    inputs.create_subscription(*wrist_args)
    assert len(executors) == 1
    inputs.create_subscription(*raw_args, raw=True, callback_group=group)
    assert nodes[0][1] is nodes[1][1]
    assert nodes[0][0].create_subscription.call_args == call(*wrist_args)
    assert nodes[1][0].create_subscription.call_args == call(*raw_args, raw=True, callback_group=group)
    inputs._start_executor()
    assert [kwargs["args"] for _, kwargs in threads] == [(executors[0],), (executors[1],)]

    inputs.set_preview_fallback("/wrist/shared", fallback_args, {})
    inputs.set_preview_fallback("/wrist/shared", fallback_args, {})
    assert nodes[0][0].create_subscription.call_args_list.count(call(*fallback_args)) == 1

    inputs.refresh(frozenset())
    for previous in executors[:2]:
        previous.shutdown.assert_called_once()
    assert all(value.join.called for value, _ in threads[:2])
    assert nodes[2][0].create_subscription.call_args_list == [call(*wrist_args), call(*fallback_args)]
    inputs.set_preview_fallback("/wrist/shared")
    nodes[2][0].destroy_subscription.assert_called_once()
    assert nodes[3][0].create_subscription.call_args == call(*raw_args, raw=True, callback_group=group)
    inputs.close()
    assert all(value.destroy_node.call_count == 1 for value, _ in nodes)
