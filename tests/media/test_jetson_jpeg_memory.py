"""Actual Jetson JPEG lifetime and color regressions; no cameras or ROS."""
import base64
import gc
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import zlib

import numpy as np

import pytest

pytest.importorskip('gi')
from insight_capture.media.jetson_preview import Decoder, OverviewEncoder

# One solid-color 1088x1920 JPEG, compressed for a small self-contained fixture.
JPEG = zlib.decompress(base64.b64decode(
    'eNrt0TdQFFEcx/H3Ntzu3d7B7bF3oI1DUtSKYGwU0FPowGwHZjswoM44I5ivw6yVgLlDwVQJoqgdRsBGxFyBWZtz'
    'lzE0Fmpj8/2996neezv/32yyN/lEhMvjZXEhpRDSXSL5WJQKVVG87UZzt27quqbpAcPwmcFAMGgFLCuUEgmHUuwU'
    'ywrHwnaaE41Gg6npGTEnI+JEHe8jUnXfaLpf1/1OyAo5f53kNWGbxjZtpiqzhGJL1ZbJLjHGnVOXIxHfIxV3Rp9h'
    '+gOWe6E9LBSpqoqmelO7p1vdc6HZeiQzv9iXVlFlZNU4BfWNTWZ2SWtHtLJnKKewurbBH4ilZ4wanTt2XN74CUWT'
    'Jk+ZOm166azZ8Tlzy8rnzV+wcNHiJUuXLV+xctXqNWvXrd+wsW7T5i3bd+zctXvP3sS+/QcOHjp85Oix5pYTJ0+d'
    'PnP23PkLbe0XL12+crXzeteNm923bt+5e+/+g4ePevv6B54OPnv+4uWr12+G3757/+Hjp89fvnq9pFDlj/y2l+32'
    'UjRN1Qyvl1TqvAu2pmfm+yLFFUZVTVpWQb3plDQ2tXb4swsrh6LVtT2BWE7RQO6wV22k2Z8Va/inZj+L/erVL4Kq'
    'dH+eaosZors5MXGoZbAzkXdcAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADg'
    'P0pN9n0DGncc/g=='
))


def dma_mappings():
    return sum(
        bool(re.match(r'^[0-9a-f]+-[0-9a-f]+ ', line)) and line.endswith('/dmabuf:')
        for line in Path('/proc/self/maps').read_text().splitlines())


@pytest.mark.skipif(os.environ.get('INSIGHT_JETSON_HARDWARE_TESTS') != '1', reason='Requires explicit Jetson hardware test run')
def test_reopening_two_frame_jpeg_preview_does_not_accumulate_device_mappings():
    counts = []
    for _ in range(30):
        decoder = Decoder('JPEG', 1088, 1920, {'x': 0, 'y': 0, 'width': 736, 'height': 1280})
        try:
            for _ in range(2):
                decoder.push(JPEG, time.monotonic_ns())
                time.sleep(1 / 30)
            deadline = time.monotonic() + 3
            while len(decoder.snapshot()) < 2 and time.monotonic() < deadline:
                time.sleep(.01)
            assert len(decoder.snapshot()) >= 2, decoder.error
        finally:
            decoder.close()
        del decoder
        gc.collect()
        time.sleep(.05)
        counts.append(dma_mappings())
    # The native task pool can lazily create a second cache after the first
    # few runs. Measure another fifteen complete lifecycles after warm-up.
    tail = counts[15:]
    assert max(tail) - min(tail) <= 2, counts


@pytest.mark.skipif(os.environ.get('INSIGHT_JETSON_HARDWARE_TESTS') != '1', reason='Requires explicit Jetson hardware test run')
def test_jpeg_colors_survive_overview_h264_encoding():
    if not shutil.which('ffmpeg'):
        pytest.skip('Requires the existing FFmpeg test decoder')
    chunks = []
    encoder = OverviewEncoder(
        {'id': '__overview__', 'width': 1280, 'height': 1280,
         'regions': [{'id': 'head', 'x': 0, 'y': 0, 'width': 736, 'height': 1280}]},
        lambda data, keyframe: chunks.append(data), lambda message: None)
    try:
        for _ in range(35):
            encoder.push('head', 'JPEG', 1088, 1920, JPEG, time.monotonic_ns())
            time.sleep(1 / 30)
        assert encoder.frames_encoded >= 25, encoder.status()
    finally:
        encoder.close()
    decoded = subprocess.run(
        ['ffmpeg', '-v', 'error', '-threads', '1', '-f', 'h264', '-i', 'pipe:0',
         '-vf', r'select=eq(n\,20)', '-frames:v', '1', '-threads', '1',
         '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
        input=b''.join(chunks), capture_output=True, timeout=10, check=True)
    rgb = np.frombuffer(decoded.stdout, np.uint8).reshape(1280, 1280, 3)
    # Independently decoded JPEG fixture center, before preview processing.
    expected = np.array([94, 147, 41])
    actual = rgb[128:1152, 128:640].mean(axis=(0, 1))
    assert np.max(np.abs(actual - expected)) < 5, actual
