"""Regression checks for USB link changes that preserve the assigned IPv4 address."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from insight_capture.runtime.discovery import CameraLink
from insight_capture.runtime.ros import domain_relay


@pytest.mark.parametrize(('indices', 'carrier_counts'), [
    ((3, 3, 7), (1, 1, 1)),
    ((3, 3, 3), (1, 1, 2)),
])
def test_same_address_after_usb_link_recovery_restarts_relay(
    tmp_path, monkeypatch, capsys, indices, carrier_counts
):
    address = '169.254.10.2'
    config = tmp_path / 'cameras.json'
    config.write_text(json.dumps({
        'ros_domain_id': 20,
        'cameras': [{'ros_domain_id': 0, 'relay_host_address': address}],
    }))
    links = iter([{CameraLink('enx020000000a01', index, address)} for index in indices])
    counts = iter(carrier_counts)
    monkeypatch.setattr(domain_relay, 'camera_link_generations',
                        lambda links: frozenset((link, next(counts)) for link in links))
    monkeypatch.setattr(domain_relay, 'local_ipv4_addresses', lambda: {address})

    def camera_links():
        try:
            return next(links)
        except StopIteration:
            raise KeyboardInterrupt

    monkeypatch.setattr(domain_relay, 'camera_links', camera_links)
    monkeypatch.setattr(domain_relay.time, 'sleep', lambda _: None)
    monkeypatch.setattr(domain_relay.signal, 'signal', lambda *_: None)
    monkeypatch.setattr('sys.argv', ['domain_relay', '--config', str(config)])
    lifecycle = []

    class Worker:
        exitcode = None

        def __init__(self, *, target, args):
            assert args[:2] == (20, 0)

        def start(self):
            lifecycle.append('start')

        def is_alive(self):
            return self.exitcode is None

        def terminate(self):
            lifecycle.append('terminate')
            self.exitcode = 0

        def join(self, *, timeout):
            pass

    monkeypatch.setattr(domain_relay.multiprocessing, 'get_context', lambda _: SimpleNamespace(Process=Worker))
    domain_relay.main()
    assert lifecycle == ['start', 'terminate', 'start', 'terminate']
    assert 'USB link changed; relay disposed' in capsys.readouterr().out


def test_camera_input_renews_after_carrier_recovers_on_same_interface(monkeypatch):
    pytest.importorskip('rclpy')
    from insight_capture.runtime import ros_inputs

    link = CameraLink('enx020000000a01', 3, '169.254.10.2')
    link_set = {link}
    counts = iter(['1\n', '2\n'])

    def read_counter(path):
        assert path == Path('/sys/class/net/enx020000000a01/carrier_up_count')
        return next(counts)

    monkeypatch.setattr(Path, 'read_text', read_counter)
    monkeypatch.setattr(ros_inputs, 'camera_links', lambda: link_set)
    waits = iter([False, True])
    renewals = []
    inputs = SimpleNamespace(
        closed=SimpleNamespace(wait=lambda _: next(waits)),
        _link_generations=ros_inputs.camera_link_generations(link_set),
        refresh=renewals.append,
    )
    ros_inputs.CameraInputs._monitor(inputs)
    assert renewals == [link_set]
