"""Native GI lifetime/backpressure regressions, with CPU-only Gst pipelines."""
import gc
import threading
from types import SimpleNamespace
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


def test_overview_waits_for_a_real_tile_before_emitting_video(cpu_pipeline):
    output = threading.Event()
    encoder = preview.OverviewEncoder(
        {'id': '__overview__', 'width': 64, 'height': 64, 'regions': [REGION]},
        lambda *_: output.set(), lambda message: pytest.fail(message))
    try:
        assert not output.wait(.12)
        encoder.push('fixture', 'NV12', 64, 64, FRAME, 1)
        assert output.wait(2)
    finally:
        encoder.close()


@pytest.mark.parametrize(('fmt', 'raw'), [('NV12', 'NV12'), ('JPEG', 'I420')])
def test_decoder_reads_fixed_plane_layout_without_parsing_caps_in_callback(monkeypatch, fmt, raw):
    parse = Gst.parse_launch

    def cpu_decoder(spec):
        source = spec.split(' caps=', 1)[0]
        return parse(source + f' caps="video/x-raw,format={raw},width=68,height=64,framerate=30/1"'
                     ' ! appsink name=output sync=false emit-signals=true')

    def reject_caps(_caps):
        raise TypeError('argument caps: Expected Gst.Caps, but got gi.repository.Gst.Caps')

    monkeypatch.setattr(Gst, 'parse_launch', cpu_decoder)
    monkeypatch.setattr(preview.GstVideo.VideoInfo, 'new_from_caps', reject_caps)
    item = preview.Decoder(fmt, 68, 64, {**REGION, 'width': 68})
    try:
        chroma_size = (68 if raw == 'NV12' else 2 * 36) * 32
        item.push(bytes([100]) * (68 * 64) + bytes([128]) * chroma_size, 1)
        deadline = time.monotonic() + 2
        frames = item.snapshot()
        while not frames and time.monotonic() < deadline:
            time.sleep(.01)
            frames = item.snapshot()
        assert len(frames) == 1
        _, _, y, uv = frames[0]
        assert (y == (100 if raw == 'NV12' else 102)).all()
        assert (uv == 128).all()
    finally:
        item.close()


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
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
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


def test_overview_holds_a_stale_tile_through_decoder_reset_and_expires_it(cpu_pipeline):
    regions = [{**REGION, 'id': name, 'x': x} for name, x in (('head', 0), ('wrist', 64))]
    encoder = preview.OverviewEncoder(
        {'id': '__overview__', 'width': 128, 'height': 64, 'regions': regions},
        lambda *_: None, lambda *_: None)

    def decoder(region, stamp, luma):
        y = preview.np.full((64, 64), luma, dtype=preview.np.uint8)
        uv = preview.np.full((32, 64), luma + 1, dtype=preview.np.uint8)
        return SimpleNamespace(x=region['x'], y=0, width=64, height=64,
                               snapshot=lambda: [(0, stamp, y, uv)], close=lambda: None)

    try:
        encoder.stopped.set()
        encoder.thread.join(timeout=1)
        now = 5_000_000_000
        assert (encoder._compose(now)[:64] == 16).all()
        assert encoder.stale == ['head', 'wrist']

        encoder.decoders['head'] = decoder(regions[0], now, 60)
        pixels = encoder._compose(now)
        assert (pixels[:64, :64] == 60).all()
        assert (pixels[64:, :64] == 61).all()
        assert encoder.stale == ['wrist']

        del encoder.decoders['head']
        gap = now + preview.STALE_NS + 1
        encoder.decoders['wrist'] = decoder(regions[1], gap, 80)
        pixels = encoder._compose(gap)
        assert (pixels[:64, :64] == 60).all()
        assert (pixels[:64, 64:] == 80).all()
        assert encoder.stale == ['head']

        expired = now + preview.OVERVIEW_HOLD_NS + 1
        pixels = encoder._compose(expired)
        assert (pixels[:64, :64] == 16).all()
        assert (pixels[64:, :64] == 128).all()

        encoder.decoders['head'] = decoder(regions[0], expired, 100)
        assert (encoder._compose(expired)[:64, :64] == 100).all()
        assert encoder.stale == ['wrist']
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
