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
