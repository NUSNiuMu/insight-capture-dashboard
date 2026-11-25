"""Regression checks for USB link changes that preserve the assigned IPv4 address."""

import json
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
    monkeypatch.setattr(domain_relay, '_carrier_up_count', lambda link: next(counts))
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
