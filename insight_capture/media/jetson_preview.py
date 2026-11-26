"""Jetson hardware decode/scale/encode with bounded NV12 overview composition.

Layout, black/neutral planes and contain-fit semantics were ported from the
independently copied Ego preview design. All hardware calls here are Jetson's.
The encoder receives frames from the preview worker.
"""
from collections import deque
import threading
import time

import cv2
import numpy as np
import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst, GstVideo
from .h264 import H264Gate
from .preview_config import FPS, GOP, OVERVIEW_HOLD_NS, PREVIEW_BITRATE, STALE_NS

Gst.init(None)

STARTUP_WAIT_NS = 500_000_000


def fit(width, height, region):
    scale = min(region["width"] / width, region["height"] / height, 1)
    # Multiple of four keeps the system-memory NV12 plane strides unambiguous.
    w, h = max(4, int(width * scale) // 4 * 4), max(4, int(height * scale) // 4 * 4)
    x = region["x"] + (region["width"] - w) // 4 * 2
    y = region["y"] + (region["height"] - h) // 4 * 2
    return x, y, w, h


# BT.601 full-range JPEG YUV -> BT.709 limited-range video YUV.
_JPEG_TO_VIDEO = (
    (219 / 255, -.1015025251475953, -.1826589439711247),
    (0, .8948050908355266, .10068400490169194),
    (0, .06592579019401852, .9006794664778416),
)


def _jpeg_video_planes(raw_y, u, v):
    height, width = raw_y.shape
    scale, a, b = _JPEG_TO_VIDEO[0]
    delta = cv2.addWeighted(u, a, v, b, 16 - 128 * (a + b), dtype=cv2.CV_32F)
    delta = cv2.resize(delta, (width, height), interpolation=cv2.INTER_NEAREST)
    y = cv2.addWeighted(raw_y, scale, delta, 1, 0, dtype=cv2.CV_8U)
    uv = np.empty((height // 2, width), np.uint8)
    for column, (_, a, b) in enumerate(_JPEG_TO_VIDEO[1:]):
        uv[:, column::2] = cv2.addWeighted(u, a, v, b, 128 * (1 - a - b))
    return y, uv


class Decoder:
    def __init__(self, fmt, width, height, region, *, software_jpeg=False):
        self.layout = (fmt, width, height)
        self.x, self.y, self.width, self.height = fit(width, height, region)
        self.frames = deque(maxlen=8)
        self.lock = threading.Lock()
        self.received = 0
        self.error = None
        self.closed = False
        self.gate = H264Gate() if fmt == "H264" else None
        if fmt == "JPEG":
            caps = f"image/jpeg,width={width},height={height},framerate={FPS}/1"
            # UQ2168 uses Y42B. Decode it to I420 without retaining hardware
            # decoder mappings when switching between overview and single cameras.
            decode = ("jpegdec idct-method=ifast ! video/x-raw,format=I420 ! " if software_jpeg
                      else "nvjpegdec ! video/x-raw,format=I420 ! ")
        elif fmt == "H264":
            caps = "video/x-h264,stream-format=byte-stream,alignment=au"
            decode = "h264parse ! nvv4l2decoder disable-dpb=true ! "
        elif fmt in {"GRAY8", "NV12"}:
            caps, decode = f"video/x-raw,format={fmt},width={width},height={height},framerate={FPS}/1", ""
            if fmt == "GRAY8":
                decode = "videoconvert ! video/x-raw,format=I420 ! "
        else:
            raise ValueError(f"Unsupported preview pixel format: {fmt}")
        # Never leak arbitrary inter frames from encoded H264. Its upstream queue
        # already sheds whole GOPs; raw/JPEG can safely replace older buffers.
        queue = "max-buffers=32 max-bytes=8388608" if fmt == "H264" else "max-buffers=2 leaky-type=downstream"
        raw_format = "I420" if fmt == "JPEG" else "NV12"
        # The output caps below fix the format and dimensions. Set up the plane
        # layout before concurrent appsink callbacks start.
        info = GstVideo.VideoInfo()
        if not info.set_format(GstVideo.VideoFormat.from_string(raw_format), self.width, self.height):
            raise RuntimeError(f"Invalid {raw_format} preview output size: {self.width}x{self.height}")
        self.plane_offsets, self.plane_strides = info.offset, info.stride
        # Separate color conversion from hardware decode/scale without a backlog.
        color_queue = ("queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 leaky=downstream ! "
                       if fmt == "JPEG" else "")
        self.pipeline = Gst.parse_launch(
            f'appsrc name=input is-live=true format=time block=false {queue} caps="{caps}" ! '
            f'{decode}nvvidconv ! video/x-raw(memory:NVMM),format={raw_format},width={self.width},height={self.height} ! '
            f'nvvidconv ! video/x-raw,format={raw_format} ! {color_queue}appsink name=output sync=false emit-signals=true max-buffers=2 drop=true')
        self.source = self.pipeline.get_by_name("input")
        self.sink = self.pipeline.get_by_name("output")
        self.sample_handler = self.sink.connect("new-sample", self._sample)
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.close()
            raise RuntimeError("Jetson preview decoder did not start")

    def _sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.EOS
        buf = sample.get_buffer()
        data = buf.extract_dup(0, buf.get_size())
        offsets, strides = self.plane_offsets, self.plane_strides
        if self.layout[0] == "JPEG":
            planes = [np.ndarray((self.height // (2 if i else 1), self.width // (2 if i else 1)),
                                 np.uint8, data, offset=offsets[i], strides=(strides[i], 1))
                      for i in range(3)]
            y, uv = _jpeg_video_planes(*planes)
        else:
            y = np.ndarray((self.height, self.width), np.uint8, data, offset=offsets[0], strides=(strides[0], 1)).copy()
            uv = np.ndarray((self.height // 2, self.width), np.uint8, data, offset=offsets[1], strides=(strides[1], 1)).copy()
        with self.lock:
            self.frames.append((int(buf.pts), time.monotonic_ns(), y, uv))
        return Gst.FlowReturn.OK

    def _check_error(self):
        message = self.pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
        if message:
            self.error = str(message.parse_error()[0])
        if self.error:
            raise RuntimeError(self.error)

    def push(self, data, stamp):
        if self.closed:
            return
        self._check_error()
        if self.gate is not None:
            data = self.gate.accept(data)
            if data is None:
                return
            # max-buffers alone only emits enough-data when block=false.
            # Reject this GOP rather than block IPC or drop reference frames.
            if (self.source.get_property("current-level-buffers") >= self.source.get_property("max-buffers") or
                    self.source.get_property("current-level-bytes") + len(data) > self.source.get_property("max-bytes")):
                self.error = "H264 preview decoder input backlog exceeded its bound"
                raise RuntimeError(self.error)
        buf = Gst.Buffer.new_wrapped(data)
        buf.pts = max(0, int(stamp))
        buf.duration = Gst.SECOND // FPS
        if self.source.emit("push-buffer", buf) != Gst.FlowReturn.OK:
            self.error = "Preview decoder rejected an input frame"
            raise RuntimeError(self.error)
        self.received += 1
        self._check_error()

    def snapshot(self):
        with self.lock:
            return list(self.frames)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.pipeline.set_state(Gst.State.NULL)
        self.sink.disconnect(self.sample_handler)
        self.pipeline = self.source = self.sink = None
        with self.lock:
            self.frames.clear()


class OverviewEncoder:
    def __init__(self, description, deliver, log, *, software_jpeg_sources=()):
        self.description, self.deliver, self.log = description, deliver, log
        self.width, self.height = description["width"], description["height"]
        self.regions = {r["id"]: r for r in description["regions"]}
        self.software_jpeg_sources = software_jpeg_sources
        self.decoders = {}
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self._close_lock = threading.Lock()
        self._closed = False
        self.error = None
        self.thread = None
        self.frames_encoded = 0
        self.stale = list(self.regions)
        self._last_tiles = {}
        # Keep motion and frequent keyframes from driving unbounded bitrate on the hotspot.
        rate = f"control-rate=1 bitrate={PREVIEW_BITRATE} preset-level=1"
        self.pipeline = Gst.parse_launch(
            f'appsrc name=input is-live=true format=time do-timestamp=true block=false max-buffers=2 leaky-type=downstream '
            f'caps="video/x-raw,format=NV12,width={self.width},height={self.height},framerate={FPS}/1" ! '
            f'nvvidconv ! video/x-raw(memory:NVMM),format=NV12 ! nvv4l2h264enc {rate} '
            f'iframeinterval={GOP} idrinterval={GOP} num-B-Frames=0 insert-sps-pps=true insert-vui=true '
            'profile=0 ! h264parse config-interval=-1 ! video/x-h264,stream-format=byte-stream,alignment=au ! '
            'appsink name=output sync=false emit-signals=true max-buffers=4 drop=false')
        self.source = self.pipeline.get_by_name("input")
        self.sink = self.pipeline.get_by_name("output")
        self.sample_handler = self.sink.connect("new-sample", self._encoded)
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.close()
            raise RuntimeError("Jetson H264 encoder did not start")
        self.thread = threading.Thread(target=self._render, daemon=True, name="preview_mosaic")
        self.thread.start()

    def _encoded(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.EOS
        buf = sample.get_buffer()
        self.frames_encoded += 1
        self.deliver(buf.extract_dup(0, buf.get_size()), not buf.has_flags(Gst.BufferFlags.DELTA_UNIT))
        return Gst.FlowReturn.OK

    def push(self, name, fmt, width, height, data, stamp):
        if name not in self.regions or self.stopped.is_set():
            return
        try:
            with self.lock:
                if self.stopped.is_set():
                    return
                decoder = self.decoders.get(name)
                if decoder and decoder.layout != (fmt, width, height):
                    decoder.close()
                    self.decoders.pop(name)
                    decoder = None
                if decoder is None:
                    decoder = self.decoders[name] = Decoder(
                        fmt, width, height, self.regions[name], software_jpeg=name in self.software_jpeg_sources,
                    )
                try:
                    decoder.push(data, stamp)
                except Exception:
                    self.decoders.pop(name, None)
                    decoder.close()
                    raise
        except Exception as exc:
            self.error = str(exc)
            self.log(f"preview decode {name}: {exc}")

    def _compose(self, now):
        with self.lock:
            decoders = dict(self.decoders)
        for name, decoder in decoders.items():
            frames = decoder.snapshot()
            if frames:
                _, received, y, uv = frames[-1]
                if y.shape == (decoder.height, decoder.width):
                    self._last_tiles[name] = received, decoder.x, decoder.y, y, uv

        pixels = np.empty((self.height * 3 // 2, self.width), np.uint8)
        pixels[:self.height].fill(16)
        pixels[self.height:].fill(128)
        stale = []
        for name in self.regions:
            tile = self._last_tiles.get(name)
            if tile is None or now - tile[0] > STALE_NS:
                stale.append(name)
            if tile is None:
                continue
            received, x, top, y, uv = tile
            if now - received > OVERVIEW_HOLD_NS:
                del self._last_tiles[name]
                continue
            height, width = y.shape
            pixels[top:top + height, x:x + width] = y
            pixels[self.height + top // 2:self.height + (top + height) // 2, x:x + width] = uv
        self.stale = stale
        return pixels

    def _render(self):
        deadline = time.monotonic()
        first_tile_ns = None
        try:
            while not self.stopped.is_set():
                now = time.monotonic_ns()
                pixels = self._compose(now)
                any_tile = len(self.stale) < len(self.regions)
                if any_tile and first_tile_ns is None:
                    first_tile_ns = now
                # Give the other cameras time to deliver their first IDR.
                partial_ready = any_tile and now - first_tile_ns >= STARTUP_WAIT_NS
                if self.frames_encoded or not self.stale or partial_ready:
                    buf = Gst.Buffer.new_wrapped(pixels.tobytes())
                    buf.duration = Gst.SECOND // FPS
                    if self.source.emit("push-buffer", buf) != Gst.FlowReturn.OK:
                        break
                message = self.pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
                if message:
                    raise RuntimeError(str(message.parse_error()[0]))
                deadline += 1 / FPS
                self.stopped.wait(max(0, deadline - time.monotonic()))
                deadline = max(deadline, time.monotonic() - 1 / FPS)
        except Exception as exc:
            self.error = str(exc)
            self.log(f"preview encode: {exc}")

    def status(self):
        return {"encoded": self.frames_encoded, "stale": self.stale, "error": self.error,
                "inputs": {name: {"received": d.received, "layout": d.layout,
                                   "last_stamp_ns": frames[-1][0] if frames else None,
                                   "age_ms": (time.monotonic_ns() - frames[-1][1]) / 1e6 if frames else None,
                                   "error": d.error}
                           for name, d in list(self.decoders.items()) for frames in [d.snapshot()]}}

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self.stopped.set()
            if self.thread is not None:
                self.thread.join(timeout=2)
            with self.lock:
                for decoder in self.decoders.values():
                    decoder.close()
                self.decoders.clear()
            self.pipeline.set_state(Gst.State.NULL)
            self.sink.disconnect(self.sample_handler)
            self.pipeline = self.source = self.sink = None
