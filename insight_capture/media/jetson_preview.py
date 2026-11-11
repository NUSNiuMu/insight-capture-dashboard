"""Jetson hardware decode/scale/encode with bounded NV12 overview composition.

Layout, black/neutral planes and contain-fit semantics were ported from the
independently copied Ego preview design. All hardware calls here are Jetson's.
There is no ROS subscription: only the dashboard's authenticated frame IPC.
"""
from collections import deque
import threading
import time

import numpy as np
import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst, GstVideo
from .preview_config import FPS, GOP, OVERVIEW_QP, SINGLE_BITRATE, SYNC_TOLERANCE_NS, SYNC_WAIT_NS, STALE_NS

Gst.init(None)


def fit(width, height, region):
    scale = min(region["width"] / width, region["height"] / height, 1)
    # Multiple of four keeps the system-memory NV12 plane strides unambiguous.
    w, h = max(4, int(width * scale) // 4 * 4), max(4, int(height * scale) // 4 * 4)
    x = region["x"] + (region["width"] - w) // 4 * 2
    y = region["y"] + (region["height"] - h) // 4 * 2
    return x, y, w, h


class Decoder:
    def __init__(self, fmt, width, height, region):
        self.layout = (fmt, width, height)
        self.x, self.y, self.width, self.height = fit(width, height, region)
        self.frames = deque(maxlen=8)
        self.lock = threading.Lock()
        self.received = 0
        self.error = None
        self.closed = False
        if fmt == "JPEG":
            caps, decode = f"image/jpeg,width={width},height={height},framerate={FPS}/1", "nvjpegdec ! "
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
        queue = "max-buffers=32" if fmt == "H264" else "max-buffers=2 leaky-type=downstream"
        self.pipeline = Gst.parse_launch(
            f'appsrc name=input is-live=true format=time block=false {queue} caps="{caps}" ! '
            f'{decode}nvvidconv ! video/x-raw(memory:NVMM),format=NV12,width={self.width},height={self.height} ! '
            'nvvidconv ! video/x-raw,format=NV12 ! appsink name=output sync=false emit-signals=true max-buffers=2 drop=true')
        self.source = self.pipeline.get_by_name("input")
        self.pipeline.get_by_name("output").connect("new-sample", self._sample)
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.close()
            raise RuntimeError("Jetson preview decoder did not start")

    def _sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.EOS
        buf = sample.get_buffer()
        info = GstVideo.VideoInfo.new_from_caps(sample.get_caps())
        data = buf.extract_dup(0, buf.get_size())
        y = np.ndarray((self.height, self.width), np.uint8, data, offset=info.offset[0], strides=(info.stride[0], 1)).copy()
        uv = np.ndarray((self.height // 2, self.width), np.uint8, data, offset=info.offset[1], strides=(info.stride[1], 1)).copy()
        with self.lock:
            self.frames.append((int(buf.pts), time.monotonic_ns(), y, uv))
        return Gst.FlowReturn.OK

    def push(self, data, stamp):
        if self.closed:
            return
        buf = Gst.Buffer.new_wrapped(data)
        buf.pts = max(0, int(stamp))
        buf.duration = Gst.SECOND // FPS
        self.source.emit("push-buffer", buf)
        self.received += 1
        message = self.pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
        if message:
            self.error = str(message.parse_error()[0])
            raise RuntimeError(self.error)

    def snapshot(self):
        with self.lock:
            return list(self.frames)

    def close(self):
        self.closed = True
        self.pipeline.set_state(Gst.State.NULL)


class OverviewEncoder:
    def __init__(self, description, deliver, log):
        self.description, self.deliver, self.log = description, deliver, log
        self.width, self.height = description["width"], description["height"]
        self.regions = {r["id"]: r for r in description["regions"]}
        self.decoders = {}
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self._close_lock = threading.Lock()
        self._closed = False
        self.error = None
        self.frames_encoded = 0
        self.paired = 0
        self.unmatched = 0
        self.last_spread_ms = None
        self.stale = list(self.regions)
        self.last = {}
        self.anchor_stamp = -1
        self.pending_anchor = None
        self.pending_since = 0
        is_overview = description["id"] == "__overview__"
        rate = (f"ratecontrol-enable=false preset-level=0 quant-i-frames={OVERVIEW_QP} quant-p-frames={OVERVIEW_QP}"
                if is_overview else f"control-rate=1 bitrate={SINGLE_BITRATE} preset-level=1")
        self.pipeline = Gst.parse_launch(
            f'appsrc name=input is-live=true format=time do-timestamp=true block=false max-buffers=2 leaky-type=downstream '
            f'caps="video/x-raw,format=NV12,width={self.width},height={self.height},framerate={FPS}/1" ! '
            f'nvvidconv ! video/x-raw(memory:NVMM),format=NV12 ! nvv4l2h264enc {rate} '
            f'iframeinterval={GOP} idrinterval={GOP} num-B-Frames=0 insert-sps-pps=true insert-vui=true '
            'profile=0 ! h264parse config-interval=-1 ! video/x-h264,stream-format=byte-stream,alignment=au ! '
            'appsink name=output sync=false emit-signals=true max-buffers=4 drop=false')
        self.source = self.pipeline.get_by_name("input")
        self.pipeline.get_by_name("output").connect("new-sample", self._encoded)
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.pipeline.set_state(Gst.State.NULL)
            raise RuntimeError("Jetson H264 encoder did not start")
        self.thread = threading.Thread(target=self._render, daemon=True, name="preview_mosaic")
        self.thread.start()

    def _encoded(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.EOS
        buf = sample.get_buffer()
        self.frames_encoded += 1
        self.deliver(buf.extract_dup(0, buf.get_size()))
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
                    decoder = self.decoders[name] = Decoder(fmt, width, height, self.regions[name])
                decoder.push(data, stamp)
        except Exception as exc:
            self.error = str(exc)
            self.log(f"preview decode {name}: {exc}")

    def _select(self, snapshots, now):
        names = list(self.regions)
        anchor_frames = snapshots.get(names[0], [])
        if anchor_frames:
            newest = anchor_frames[-1]
            if self.pending_anchor is None and newest[0] != self.anchor_stamp:
                self.pending_anchor, self.pending_since = newest, now
        if self.pending_anchor is not None:
            anchor = self.pending_anchor
            matches = {name: min(frames, key=lambda f: abs(f[0] - anchor[0]))
                       for name, frames in snapshots.items() if frames}
            valid = {name: f for name, f in matches.items()
                     if abs(f[0] - anchor[0]) <= SYNC_TOLERANCE_NS and now - f[1] <= STALE_NS}
            if len(valid) == len(names) or now - self.pending_since >= SYNC_WAIT_NS:
                self.last.update(valid)
                if len(valid) == len(names):
                    self.paired += 1
                    self.last_spread_ms = (max(f[0] for f in valid.values()) - min(f[0] for f in valid.values())) / 1e6
                else:
                    self.unmatched += 1
                self.anchor_stamp, self.pending_anchor = anchor[0], None
        # A failed head must not freeze the other live cameras indefinitely.
        if not anchor_frames or now - anchor_frames[-1][1] > STALE_NS:
            for name, frames in snapshots.items():
                if frames:
                    self.last[name] = frames[-1]
        return {name: f for name, f in self.last.items() if now - f[1] <= STALE_NS}

    def _render(self):
        deadline = time.monotonic()
        try:
            while not self.stopped.is_set():
                now = time.monotonic_ns()
                with self.lock:
                    decoders = dict(self.decoders)
                selected = self._select({name: d.snapshot() for name, d in decoders.items()}, now)
                pixels = np.empty((self.height * 3 // 2, self.width), np.uint8)
                pixels[:self.height].fill(16)
                pixels[self.height:].fill(128)
                self.stale = [name for name in self.regions if name not in selected]
                for name, frame in selected.items():
                    d = decoders.get(name)
                    if d is None:
                        continue
                    _, _, y, uv = frame
                    if y.shape != (d.height, d.width):
                        continue
                    pixels[d.y:d.y + d.height, d.x:d.x + d.width] = y
                    pixels[self.height + d.y // 2:self.height + (d.y + d.height) // 2, d.x:d.x + d.width] = uv
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
        return {"encoded": self.frames_encoded, "paired": self.paired, "unmatched": self.unmatched,
                "pair_spread_ms": self.last_spread_ms, "stale": self.stale, "error": self.error,
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
            self.thread.join(timeout=2)
            with self.lock:
                for decoder in self.decoders.values():
                    decoder.close()
                self.decoders.clear()
            self.pipeline.set_state(Gst.State.NULL)
