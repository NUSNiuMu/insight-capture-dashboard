"""Real clock steps, sample uncertainty, and work after receipt are distinct."""
import json
import struct

import pytest

from insight_capture.runtime.recording import clock_sync as clocks


class ReplySocket:
    def __init__(self, clock):
        self.clock = clock
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.clock['elapsed'] += .05  # Arbitrary scheduling/close delay.
    def settimeout(self, *args):
        pass
    def connect(self, *args):
        pass
    def setsockopt(self, *args):
        pass
    def send(self, packet):
        self.packet = packet
    def recvmsg(self, *args):
        self.clock['elapsed'] = .004
        reply = bytearray(48)
        reply[0], reply[1] = 0x24, 11
        reply[24:32] = self.packet[40:48]
        for index, value in [(32, 1000.001), (40, 1000.002)]:
            value += clocks.NTP_EPOCH
            struct.pack_into('!II', reply, index, int(value), int(value % 1 * 2**32))
        return reply, [], 0, None


def test_socket_close_delay_is_not_a_wall_clock_step(monkeypatch):
    clock = {'elapsed': 0.}
    monkeypatch.setattr(clocks.socket, 'socket', lambda *args: ReplySocket(clock))
    monkeypatch.setattr(clocks.time, 'time', lambda: 1000 + clock['elapsed'])
    monkeypatch.setattr(clocks.time, 'monotonic', lambda: clock['elapsed'])
    sample = clocks.ntp_sample('192.0.2.1')
    assert sample['round_trip_ms'] < 3.1
    assert sample['offset_bound_ms'] < 2.1
    assert clock['elapsed'] == pytest.approx(.054)


@pytest.mark.parametrize('where', ['send', 'receive'])
def test_delayed_clock_pair_is_inconclusive_not_a_reported_clock_step(monkeypatch, where):
    clock = {'elapsed': 0.}
    mono = iter([0, .02] if where == 'send' else [0, 0, .004, .024])
    monkeypatch.setattr(clocks.socket, 'socket', lambda *args: ReplySocket(clock))
    monkeypatch.setattr(clocks.time, 'time', lambda: 1000 + clock['elapsed'])
    monkeypatch.setattr(clocks.time, 'monotonic', lambda: next(mono))
    with pytest.raises(ValueError, match='sampling was delayed'):
        clocks.ntp_sample('192.0.2.1')


@pytest.mark.parametrize('step', [-.02, .02])
def test_actual_wall_clock_step_remains_invalid(monkeypatch, step):
    clock = {'elapsed': 0.}
    monkeypatch.setattr(clocks.socket, 'socket', lambda *args: ReplySocket(clock))
    monkeypatch.setattr(clocks.time, 'monotonic', lambda: clock['elapsed'])
    monkeypatch.setattr(clocks.time, 'time', lambda: 1000 + clock['elapsed'] + (step if clock['elapsed'] else 0))
    with pytest.raises(ValueError, match='wall clock stepped'):
        clocks.ntp_sample('192.0.2.1')


@pytest.mark.parametrize('fault', [None, 'wrong_id', 'clock_step'])
def test_ego_http_clock_probe_validates_and_bounds_fresh_sample(monkeypatch, fault):
    elapsed = [0.0]
    monkeypatch.setattr(clocks.time, 'monotonic', lambda: elapsed[0])
    monkeypatch.setattr(clocks.time, 'time',
                        lambda: 1000 + elapsed[0] + (.02 if fault == 'clock_step' and elapsed[0] > .025 else 0))

    class Connection:
        def __init__(self, host, *, timeout):
            assert (host, timeout) == ('169.254.10.1', .5)

        def connect(self):
            elapsed[0] += .025

        def request(self, method, path, *, body, headers):
            assert (method, path) == ('POST', '/api/time-sync/ping')
            assert headers['Content-Type'] == 'application/json'
            self.request_body = json.loads(body)
            assert self.request_body['clientTime'] == 1000025

        def getresponse(self):
            return self

        def read(self):
            elapsed[0] += .004
            return json.dumps({'timestamp': self.request_body['clientTime'],
                               'measurementId': self.request_body['measurementId'] + (fault == 'wrong_id'),
                               'serverTimeNanos': 1_000_028_000_000}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            elapsed[0] += .050

        status = 200

        def close(self):
            elapsed[0] += .050

    monkeypatch.setattr(clocks, 'HTTPConnection', Connection, raising=False)
    monkeypatch.setattr(clocks, 'json_request',
                        lambda *args, **kwargs: pytest.fail('The clock probe must connect before timing'))
    if fault:
        with pytest.raises(ValueError, match='Invalid EgoHalo|wall clock stepped'):
            clocks.ego_http_clock_sample('169.254.10.1')
    else:
        result = clocks.ego_http_clock_sample('169.254.10.1')
        assert result['offset_ms'] == 1
        assert result['round_trip_ms'] == 4
        assert result['offset_bound_ms'] == 3
    assert elapsed[0] == pytest.approx(.129)


@pytest.mark.parametrize('bound,delay,passing', [(2, 4, True), (9.5, 4, False), (6, 12, False)])
def test_only_ego_prefers_http_and_keeps_recording_limits(monkeypatch, bound, delay, passing):
    ego, wrist = '169.254.10.1', '169.254.20.1'
    config = {'ego_url': f'http://{ego}:8765', 'max_pairwise_clock_bound_ms': 10, 'ntp_samples': 1,
              'clock_devices': [{'name': 'EgoHalo', 'host': ego}, {'name': 'Wrist', 'host': wrist}]}

    def ntp(host):
        if host == ego:
            raise ConnectionRefusedError('no UDP listener')
        return {'host': host, 'offset_bound_ms': 1, 'round_trip_ms': 1}

    def http(host):
        assert host == ego
        return {'host': host, 'method': 'HTTP', 'offset_bound_ms': bound, 'round_trip_ms': delay}

    monkeypatch.setattr(clocks, 'ntp_sample', ntp)
    monkeypatch.setattr(clocks, 'ego_http_clock_sample', http)
    report = clocks.measure_clocks(config)
    assert report['ok'] is passing
    assert report['devices']['EgoHalo']['method'] == 'HTTP'
    assert report['devices']['Wrist']['ok']
    monkeypatch.setattr(clocks, 'ntp_sample', lambda host: ntp(ego))
    assert not clocks.measure_clocks(config)['devices']['Wrist']['ok']


def test_failed_ego_fallback_reports_both_transports(monkeypatch):
    def no_ntp(host):
        raise ConnectionRefusedError('no UDP listener')

    def no_http(host):
        raise ValueError('bad timestamp')

    monkeypatch.setattr(clocks, 'ntp_sample', no_ntp)
    monkeypatch.setattr(clocks, 'ego_http_clock_sample', no_http)
    report = clocks.measure_clocks({
        'ego_url': 'http://169.254.10.1:8765', 'ntp_samples': 1,
        'clock_devices': [{'name': 'EgoHalo', 'host': '169.254.10.1'}],
    })
    assert not report['ok']
    assert report['devices']['EgoHalo']['error'] == (
        'UDP NTP unavailable (no UDP listener); EgoHalo OTA clock probe failed: bad timestamp')


@pytest.mark.parametrize('failures,passing', [
    ((ConnectionRefusedError, TimeoutError), False),
    ((TimeoutError, ValueError), False),
    ((ConnectionRefusedError, None), True),
])
def test_older_ego_without_http_probe_falls_back_to_udp(monkeypatch, failures, passing):
    host = '169.254.10.1'
    errors = iter(failures)
    ntp_result = {'host': host, 'offset_bound_ms': 1, 'round_trip_ms': 2}
    calls = []

    def ntp(hostname):
        calls.append('udp')
        failure = next(errors)
        if failure:
            raise failure(failure.__name__)
        return dict(ntp_result)

    def http(hostname):
        calls.append('http')
        raise ValueError('HTTP 404')

    monkeypatch.setattr(clocks, 'ntp_sample', ntp)
    monkeypatch.setattr(clocks, 'ego_http_clock_sample', http)
    report = clocks.measure_clocks({
        'ego_url': f'http://{host}:8765', 'ntp_samples': 2,
        'clock_devices': [{'name': 'EgoHalo', 'host': host}],
    })
    assert calls == ['http', 'udp', 'udp']
    assert report['ok'] is passing
