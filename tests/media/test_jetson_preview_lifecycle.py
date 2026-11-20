"""Native GI lifetime/backpressure regressions, with CPU-only Gst pipelines."""
import gc
import time
import weakref

import pytest

pytest.importorskip('gi')
from insight_capture.media import jetson_preview as preview

Gst = preview.Gst
REGION = {'id': 'fixture', 'x': 0, 'y': 0, 'width': 64, 'height': 64}
FRAME = bytes(64 * 64 * 3 // 2)
SPS_PPS_IDR = b'\x00\x00\x00\x01\x67s\x00\x00\x00\x01\x68p\x00\x00\x00\x01\x65i'
INTER = b'\x00\x00\x00\x01\x41p'


@pytest.fixture
def cpu_pipeline(monkeypatch):
    parse = Gst.parse_launch

    def build(spec):
        source = spec.split(' ! ', 1)[0].replace(
            'caps="video/x-h264,stream-format=byte-stream,alignment=au"',
            'caps="video/x-raw,format=NV12,width=64,height=64,framerate=30/1"')
        return parse(source + ' ! appsink name=output sync=false emit-signals=true max-buffers=2 drop=true')

    monkeypatch.setattr(Gst, 'parse_launch', build)


def collect():
    for _ in range(3):
        gc.collect()
        time.sleep(.02)


@pytest.mark.parametrize('kind', ['decoder', 'encoder'])
def test_closed_objects_are_released_after_native_callbacks_disconnect(cpu_pipeline, kind):
    references = []
    for _ in range(3):
        if kind == 'decoder':
            item = preview.Decoder('NV12', 64, 64, REGION)
            item.push(FRAME, 1)
        else:
            item = preview.OverviewEncoder(
                {'id': '__overview__', 'width': 64, 'height': 64, 'regions': [REGION]},
                lambda *_: None, lambda *_: None)
        time.sleep(.03)
        item.close()
        item.close()
        references.append(weakref.ref(item))
        del item
    collect()
    assert all(reference() is None for reference in references)


@pytest.mark.parametrize('kind', ['decoder', 'encoder'])
def test_failed_start_releases_its_native_callback(monkeypatch, cpu_pipeline, kind):
    original_parse = Gst.parse_launch
    references = []

    def fail_start(spec):
        pipe = original_parse(spec)
        set_state = pipe.set_state
        pipe.set_state = lambda state: Gst.StateChangeReturn.FAILURE if state == Gst.State.PLAYING else set_state(state)
        return pipe

    base = preview.Decoder if kind == 'decoder' else preview.OverviewEncoder

    class Observed(base):
        def __init__(self):
            references.append(weakref.ref(self))
            if kind == 'decoder':
                super().__init__('NV12', 64, 64, REGION)
            else:
                super().__init__({'id': '__overview__', 'width': 64, 'height': 64,
                                  'regions': [REGION]}, lambda *_: None, lambda *_: None)

    monkeypatch.setattr(Gst, 'parse_launch', fail_start)

    def attempt():
        try:
            Observed()
        except RuntimeError:
            return
        raise AssertionError('Expected failed decoder start')

    attempt()
    collect()
    assert references[0]() is None


def test_h264_stalled_consumer_has_a_hard_queue_bound(monkeypatch):
    parse = Gst.parse_launch

    def stalled(spec):
        source = spec.split(' ! ', 1)[0].replace(
            'caps="video/x-h264,stream-format=byte-stream,alignment=au"',
            'caps="video/x-raw,format=NV12,width=64,height=64,framerate=30/1"')
        return parse(source + ' ! appsink name=output sync=false emit-signals=false max-buffers=1 drop=false')

    monkeypatch.setattr(Gst, 'parse_launch', stalled)
    item = preview.Decoder('H264', 64, 64, REGION)
    rejected = False
    try:
        for i in range(120):
            data = (SPS_PPS_IDR if i == 0 else INTER).ljust(len(FRAME), b'\0')
            try:
                item.push(data, i)
            except RuntimeError:
                rejected = True
                break
        assert rejected
        assert item.source.get_property('current-level-buffers') <= 32
    finally:
        item.close()


def test_failed_decoder_is_removed_and_replacement_waits_for_an_idr(monkeypatch, cpu_pipeline):
    base = preview.Decoder
    created = []

    class FailingFirst(base):
        def __init__(self, *args):
            super().__init__(*args)
            created.append(self)

        def push(self, data, stamp):
            if len(created) == 1:
                raise RuntimeError('Fixture pipeline failure')
            return super().push(data, stamp)

    monkeypatch.setattr(preview, 'Decoder', FailingFirst)
    owner = preview.OverviewEncoder(
        {'id': '__overview__', 'width': 64, 'height': 64, 'regions': [REGION]},
        lambda *_: None, lambda *_: None)
    try:
        owner.push('fixture', 'H264', 64, 64, SPS_PPS_IDR.ljust(len(FRAME), b'\0'), 0)
        assert created[0].closed
        assert not owner.decoders
        owner.push('fixture', 'H264', 64, 64, INTER.ljust(len(FRAME), b'\0'), 1)
        assert created[1].received == 0
        owner.push('fixture', 'H264', 64, 64, SPS_PPS_IDR.ljust(len(FRAME), b'\0'), 2)
        assert created[1].received == 1
    finally:
        owner.close()


def test_overview_shows_latest_live_tiles_without_waiting_for_a_match(cpu_pipeline):
    regions = [{**REGION, 'id': name} for name in ('head', 'wrist', 'stale')]
    encoder = preview.OverviewEncoder(
        {'id': '__overview__', 'width': 64, 'height': 64, 'regions': regions},
        lambda *_: None, lambda *_: None)
    try:
        encoder.stopped.set()
        encoder.thread.join(timeout=1)
        now = 1_000_000_000
        head = (200_000_000, now, None, None)
        wrist = (100_000_000, now - 5_000_000, None, None)
        stale = (head[0], now - preview.STALE_NS - 1, None, None)
        assert encoder._select({
            'head': [(wrist[0], wrist[1], None, None), head],
            'wrist': [wrist],
            'stale': [stale],
        }, now) == {'head': head, 'wrist': wrist}
    finally:
        encoder.close()


@pytest.mark.parametrize('selection', [False, True])
def test_h264_selection_probe_does_not_retain_closed_decoder(monkeypatch, selection):
    from insight_capture.media.h264_decode import H264Decoder
    parse = Gst.parse_launch
    monkeypatch.setattr(Gst, 'parse_launch', lambda _: parse(
        'appsrc name=source is-live=true format=time '
        'caps=video/x-raw,format=NV12,width=64,height=64,framerate=30/1 ! '
        'identity name=decoder ! appsink name=sink sync=false max-buffers=1 drop=true'))
    references = []
    for _ in range(3):
        item = H264Decoder(hardware=False, frame_selector=(lambda _: True) if selection else None)
        item.close()
        item.close()
        references.append(weakref.ref(item))
        del item
    collect()
    assert all(reference() is None for reference in references)
