"""Real clock steps, sample uncertainty, and work after receipt are distinct."""
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
    readings = iter([(1000.0, 0.0, 0.0),
                     (1000.024 if fault == 'clock_step' else 1000.004, .004, .004)])
    monkeypatch.setattr(clocks, '_wall_clock_sample', lambda: next(readings))

    def request(url, body, *, timeout):
        assert url == 'http://169.254.10.1/api/time-sync/ping'
        assert body['clientTime'] == 1000000
        return {'timestamp': body['clientTime'],
                'measurementId': body['measurementId'] + (fault == 'wrong_id'),
                'serverTimeNanos': 1_000_003_000_000}

    monkeypatch.setattr(clocks, 'json_request', request)
    if fault:
        with pytest.raises(ValueError, match='Invalid EgoHalo|wall clock stepped'):
            clocks.ego_http_clock_sample('169.254.10.1')
    else:
        result = clocks.ego_http_clock_sample('169.254.10.1')
        assert result['offset_ms'] == 1
        assert result['round_trip_ms'] == 4
        assert result['offset_bound_ms'] == 3


@pytest.mark.parametrize('bound,delay,passing', [(2, 4, True), (9.5, 4, False), (6, 12, False)])
def test_only_client_ego_falls_back_and_keeps_recording_limits(monkeypatch, bound, delay, passing):
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


@pytest.mark.parametrize('failures,use_http', [
    ((ConnectionRefusedError, TimeoutError), True),
    ((TimeoutError, TimeoutError), True),
    ((TimeoutError, ValueError), False),
    ((ConnectionRefusedError, None), False),
])
def test_ego_http_fallback_requires_no_usable_udp_reply(monkeypatch, failures, use_http):
    host = '169.254.10.1'
    errors = iter(failures)
    ntp_result = {'host': host, 'offset_bound_ms': 1, 'round_trip_ms': 2}
    http_calls = []

    def ntp(hostname):
        failure = next(errors)
        if failure:
            raise failure(failure.__name__)
        return dict(ntp_result)

    def http(hostname):
        http_calls.append(hostname)
        return {**ntp_result, 'method': 'HTTP'}

    monkeypatch.setattr(clocks, 'ntp_sample', ntp)
    monkeypatch.setattr(clocks, 'ego_http_clock_sample', http)
    report = clocks.measure_clocks({
        'ego_url': f'http://{host}:8765', 'ntp_samples': 2,
        'clock_devices': [{'name': 'EgoHalo', 'host': host}],
    })
    assert http_calls == ([host] * 2 if use_http else [])
    assert report['ok'] == (use_http or None in failures)
