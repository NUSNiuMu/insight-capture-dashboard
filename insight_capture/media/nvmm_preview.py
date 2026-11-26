"""Jetson H.264 overview with decoded frames kept in NVIDIA video memory."""

from collections import OrderedDict
from threading import Lock
import time
from types import SimpleNamespace

import gi
gi.require_version("GstBase", "1.0")
from gi.repository import GstBase

from .h264 import H264Gate
from .jetson_preview import Gst
from .preview_config import FPS, GOP, OVERVIEW_HOLD_NS, PREVIEW_BITRATE, STALE_NS

STARTUP_WAIT_NS = 2_000_000_000
DECODER_QUEUE_FRAMES = 32
DECODER_QUEUE_BYTES = 8 * 1024 * 1024
STARTUP_QUEUE_FRAMES = FPS * 3
STARTUP_QUEUE_BYTES = 3 * DECODER_QUEUE_BYTES


class NvmmOverviewEncoder:
    native_overview = True

    def __init__(self, description, deliver, log):
        self.deliver = deliver
        self.log = log
        self._push_lock = Lock()
        self._closed = False
        self.error = None
        self.frames_encoded = 0
        self._published = False
        self._first_tile_at = None
        self._ready_pts = None
        self._tiles = {}
        self._signals = []
        self._probes = []
        self.inputs = {}
        geometry, streams = [], []
        for index, tile in enumerate(description["regions"]):
            pad = f"sink_{index}"
            geometry.append(
                f"{pad}::xpos={tile['x']} {pad}::ypos={tile['y']} "
                f"{pad}::width={tile['width']} {pad}::height={tile['height']} "
                f"{pad}::max-last-buffer-repeat={OVERVIEW_HOLD_NS}"
            )
            streams.append(
                f'appsrc name=input{index} is-live=true format=time block=false max-buffers={STARTUP_QUEUE_FRAMES} '
                f'max-bytes={STARTUP_QUEUE_BYTES} caps="video/x-h264,stream-format=byte-stream,alignment=au,framerate={FPS}/1" '
                f'! h264parse ! nvv4l2decoder name=decode{index} disable-dpb=true num-extra-surfaces=8 '
                f'! queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! mix.{pad}'
            )
        width, height = description["width"], description["height"]
        output = (
            'nvcompositor name=mix emit-signals=true start-time-selection=first latency=0 '
            + " ".join(geometry)
            + f' ! video/x-raw(memory:NVMM),format=RGBA,width={width},height={height},framerate={FPS}/1 '
            '! nvvidconv ! video/x-raw(memory:NVMM),format=NV12 '
            f'! nvv4l2h264enc control-rate=1 bitrate={PREVIEW_BITRATE} preset-level=1 '
            f'iframeinterval={GOP} idrinterval={GOP} num-B-Frames=0 insert-sps-pps=true insert-vui=true '
            'profile=0 ! h264parse config-interval=-1 '
            '! video/x-h264,stream-format=byte-stream,alignment=au '
            '! appsink name=output sync=false emit-signals=true max-buffers=4 drop=false'
        )
        self.pipeline = Gst.parse_launch(" ".join([output, *streams]))
        try:
            self.mixer = self.pipeline.get_by_name("mix")
            GstBase.Aggregator.set_ignore_inactive_pads(self.mixer, True)
            self._signals.append((self.mixer, self.mixer.connect("samples-selected", self._selected)))
            for index, tile in enumerate(description["regions"]):
                input_stream = SimpleNamespace(
                    source=self.pipeline.get_by_name(f"input{index}"),
                    pad=self.mixer.get_static_pad(f"sink_{index}"),
                    gate=H264Gate(), pending=OrderedDict(), decoded=OrderedDict(),
                    layout=None, received=0, last_input_ns=None, last_pts=-1,
                )
                decoder = self.pipeline.get_by_name(f"decode{index}")
                output_pad = decoder.get_static_pad("src")
                probe = output_pad.add_probe(Gst.PadProbeType.BUFFER, self._decoded, input_stream)
                self._probes.append((output_pad, probe))
                self.inputs[tile["id"]] = input_stream
            sink = self.pipeline.get_by_name("output")
            self._signals.append((sink, sink.connect("new-sample", self._encoded)))
            if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("NVMM preview did not start")
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _remember(history, key, value):
        history[key] = value
        if len(history) > 120:
            history.popitem(last=False)

    def _running_time(self):
        clock = self.pipeline.get_clock()
        return max(0, clock.get_time() - self.pipeline.get_base_time()) if clock else 0

    def _check_error(self):
        message = self.pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
        if message and self.error is None:
            self.error = str(message.parse_error()[0])
            self.log(f"NVMM preview: {self.error}")

    def push(self, name, fmt, width, height, data, stamp):
        stream = self.inputs.get(name)
        if stream is None or fmt != "H264":
            return
        with self._push_lock:
            if self._closed or self.error:
                return
            self._check_error()
            if self.error:
                return
            now = time.monotonic_ns()
            if stream.last_input_ns is not None and now - stream.last_input_ns > STALE_NS:
                stream.gate.reset()
            stream.last_input_ns = now
            data = stream.gate.accept(data)
            if data is None:
                return
            source = stream.source
            # The decoder can hold its compressed inputs while the first caps negotiate.
            startup = self._ready_pts is None
            max_buffers = STARTUP_QUEUE_FRAMES if startup else DECODER_QUEUE_FRAMES
            max_bytes = STARTUP_QUEUE_BYTES if startup else DECODER_QUEUE_BYTES
            buffered = source.get_property("current-level-buffers")
            if buffered >= max_buffers or source.get_property("current-level-bytes") + len(data) > max_bytes:
                self.error = f"{name}: NVMM preview decoder input exceeded its queue bound"
                self.log(self.error)
                return
            buffer = Gst.Buffer.new_wrapped(data)
            pts = max(self._running_time(), stream.last_pts + 1)
            stream.last_pts = pts
            self._remember(stream.pending, pts, stamp)
            buffer.pts = pts
            buffer.duration = Gst.SECOND // FPS
            if source.emit("push-buffer", buffer) == Gst.FlowReturn.OK:
                stream.received += 1
                stream.layout = (fmt, width, height)
            else:
                self.error = f"{name}: NVMM preview decoder rejected a frame"
                self.log(self.error)

    def _decoded(self, _pad, info, stream):
        buffer = info.get_buffer()
        if buffer is not None:
            stamp = stream.pending.pop(int(buffer.pts), None)
            if stamp is not None:
                # Pick the freshest decoded tile instead of waiting on old input timestamps.
                buffer.pts = self._running_time()
                self._remember(stream.decoded, int(buffer.pts), (stamp, time.monotonic_ns()))
        return Gst.PadProbeReturn.OK

    def _selected(self, mixer, _segment, pts, _dts, _duration, _info):
        tiles = {}
        for name, stream in self.inputs.items():
            sample = mixer.peek_next_sample(stream.pad)
            if sample and sample.get_buffer():
                tile = stream.decoded.get(int(sample.get_buffer().pts))
                if tile is not None:
                    tiles[name] = tile
        self._tiles = tiles
        if self._ready_pts is None and tiles:
            now = time.monotonic_ns()
            if self._first_tile_at is None:
                self._first_tile_at = now
            # Discovery and the first keyframe can arrive separately for each camera.
            awaiting_live_tile = any(
                name not in tiles and stream.last_input_ns is not None and now - stream.last_input_ns <= STALE_NS
                for name, stream in self.inputs.items()
            )
            missing_tile_timeout = now - self._first_tile_at >= STARTUP_WAIT_NS and not awaiting_live_tile
            if len(tiles) == len(self.inputs) or missing_tile_timeout:
                self._ready_pts = pts

    def _encoded(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.EOS
        buffer = sample.get_buffer()
        keyframe = not buffer.has_flags(Gst.BufferFlags.DELTA_UNIT)
        if not self._published:
            if self._ready_pts is None or buffer.pts < self._ready_pts or not keyframe:
                return Gst.FlowReturn.OK
            self._published = True
        self.frames_encoded += 1
        self.deliver(buffer.extract_dup(0, buffer.get_size()), keyframe)
        return Gst.FlowReturn.OK

    def status(self):
        self._check_error()
        tiles, now = self._tiles, time.monotonic_ns()
        inputs = {}
        stale = []
        for name, stream in self.inputs.items():
            stamp, decoded = tiles.get(name, (None, None))
            age = (now - decoded) / 1e6 if decoded is not None else None
            if decoded is None or now - decoded > STALE_NS:
                stale.append(name)
            inputs[name] = {"received": stream.received, "layout": stream.layout,
                            "last_stamp_ns": stamp, "age_ms": age, "error": None}
        return {"compositor": "nvmm", "encoded": self.frames_encoded, "stale": stale,
                "error": self.error, "inputs": inputs}

    def close(self):
        with self._push_lock:
            if self._closed:
                return
            self._closed = True
        self.pipeline.set_state(Gst.State.NULL)
        for pad, probe in self._probes:
            pad.remove_probe(probe)
        for element, signal in self._signals:
            element.disconnect(signal)
