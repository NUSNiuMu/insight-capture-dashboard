#!/usr/bin/env python3

"""Jetson hardware JPEG codec (nvjpegenc/nvjpegdec) behind a CPU-fallback API.

The display encode path previously paid two CPU costs per raw frame:
NV12->BGR cvtColor plus cv2.imencode. The NVJPEG engine takes the ROS
message's NV12 (or mono8) bytes directly -- color conversion happens in
nvvidconv on the way into NVMM memory -- so the CPU's only remaining work
is one buffer copy. Same idea for the hand-overlay path, which decodes and
re-encodes a JPEG per frame while the Settings toggle is on.

Every entry point returns None on any failure and the caller keeps its
existing cv2 path, so a PC without NVIDIA GStreamer elements (or a broken
pipeline mid-run) degrades to exactly the old behavior. A pipeline that
fails repeatedly is torn down and its key disabled for the process
lifetime rather than retried every frame.

Pipelines are persistent (appsrc -> ... -> appsink, one per caller key +
caps) because building one costs ~100ms while pushing a buffer through an
existing one costs ~1-3ms; per-key also means the two infrared cameras
never serialize behind a shared pipeline lock.
"""

import threading
from typing import Dict, Optional, Tuple

import numpy as np

try:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
except Exception:  # pragma: no cover - non-Jetson dev machines
    Gst = None

_REQUIRED_ELEMENTS = ("nvjpegenc", "nvjpegdec", "nvvidconv")
# Consecutive per-key failures before the key is disabled for good. One
# rebuild attempt happens in between, so a transient EOS/flush hiccup
# recovers but a systematically broken caps combination stops spamming.
_MAX_KEY_FAILURES = 3
_PULL_TIMEOUT_NS = int(1e9)


class _Pipeline:
    """One persistent appsrc->appsink pipeline plus the lock guarding it.

    GStreamer elements are not thread-safe for interleaved push/pull from
    multiple threads; each camera worker owns its own key so in practice
    the lock is uncontended.
    """

    def __init__(self, description: str, out_caps_note: str) -> None:
        self.lock = threading.Lock()
        self.pipeline = Gst.parse_launch(description)
        self.src = self.pipeline.get_by_name("src")
        self.sink = self.pipeline.get_by_name("sink")
        self.out_caps_note = out_caps_note
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            self.pipeline.set_state(Gst.State.NULL)
            raise RuntimeError(f"pipeline failed to start: {description}")

    def process(self, data: bytes) -> Optional[Tuple[bytes, object]]:
        """Push one buffer, pull one sample; returns (bytes, caps) or None."""
        with self.lock:
            buf = Gst.Buffer.new_wrapped(data)
            if self.src.emit("push-buffer", buf) != Gst.FlowReturn.OK:
                return None
            sample = self.sink.emit("try-pull-sample", _PULL_TIMEOUT_NS)
            if sample is None:
                return None
            out = sample.get_buffer()
            ok, mapinfo = out.map(Gst.MapFlags.READ)
            if not ok:
                return None
            try:
                return bytes(mapinfo.data), sample.get_caps()
            finally:
                out.unmap(mapinfo)

    def destroy(self) -> None:
        try:
            self.pipeline.set_state(Gst.State.NULL)
        except Exception:
            pass


class HwJpegCodec:
    """Keyed registry of hardware encode/decode pipelines. Use create()."""

    def __init__(self, log=print) -> None:
        self._log = log
        self._pipelines: Dict[Tuple, _Pipeline] = {}
        self._failures: Dict[Tuple, int] = {}
        self._registry_lock = threading.Lock()

    @classmethod
    def create(cls, log=print) -> Optional["HwJpegCodec"]:
        if Gst is None:
            log("hw_jpeg: PyGObject/GStreamer not importable; using CPU JPEG codec")
            return None
        try:
            if not Gst.is_initialized():
                Gst.init(None)
        except Exception as exc:
            log(f"hw_jpeg: Gst.init failed ({exc}); using CPU JPEG codec")
            return None
        missing = [name for name in _REQUIRED_ELEMENTS if Gst.ElementFactory.find(name) is None]
        if missing:
            log(f"hw_jpeg: missing GStreamer elements {missing}; using CPU JPEG codec")
            return None
        log("hw_jpeg: NVJPEG hardware JPEG encode/decode enabled")
        return cls(log=log)

    # ── ROS raw-image encode ────────────────────────────────────────────────

    @staticmethod
    def ros_image_layout(msg) -> Optional[Tuple[str, int, int]]:
        """Map a sensor_msgs/Image to (gst_format, width, luma_height), or
        None when the hardware path can't take this message directly.
        """
        width = int(getattr(msg, "width", 0))
        data_len = len(msg.data)
        if width <= 0 or data_len == 0:
            return None
        encoding = msg.encoding.lower()
        if encoding in ("mono8", "8uc1"):
            if data_len == width * int(msg.height):
                return ("GRAY8", width, int(msg.height))
            return None
        if encoding == "nv12":
            # Mirror _decode_calibration_message: some drivers report
            # msg.height as the full Y+UV buffer height, so derive the luma
            # height from the data length instead of trusting msg.height.
            total_rows, remainder = divmod(data_len, width)
            if remainder == 0 and total_rows > 0 and total_rows % 3 == 0:
                return ("NV12", width, total_rows * 2 // 3)
            return None
        return None

    def can_encode_ros_image(self, key: str, msg) -> bool:
        layout = self.ros_image_layout(msg)
        if layout is None:
            return False
        fmt, width, height = layout
        return self._failures.get(("enc", key, fmt, width, height), 0) < _MAX_KEY_FAILURES

    def encode_ros_image(self, key: str, msg, quality: int = 82) -> Optional[Tuple[bytes, int, int]]:
        """Encode a mono8/NV12 sensor_msgs/Image straight from msg.data.

        Returns (jpeg_bytes, width, height) or None (caller falls back).
        """
        layout = self.ros_image_layout(msg)
        if layout is None:
            return None
        fmt, width, height = layout
        jpeg = self._encode(key, bytes(msg.data), fmt, width, height, quality)
        if jpeg is None:
            return None
        return jpeg, width, height

    # ── BGRx round-trip for the hand-overlay draw path ──────────────────────

    def decode_jpeg_bgrx(self, key: str, jpeg: bytes) -> Optional[np.ndarray]:
        """Decode a JPEG to a writable HxWx4 BGRx array (cv2 draws on 4
        channels fine, and BGRx feeds back into encode_bgrx without another
        color conversion)."""
        pipe_key = ("dec", key)
        result = self._run(
            pipe_key,
            lambda: _Pipeline(
                "appsrc name=src is-live=true format=time do-timestamp=true "
                "caps=image/jpeg ! nvjpegdec ! nvvidconv ! "
                "video/x-raw,format=BGRx ! appsink name=sink sync=false max-buffers=2 drop=true",
                "BGRx",
            ),
            jpeg,
        )
        if result is None:
            return None
        data, caps = result
        structure = caps.get_structure(0)
        width = structure.get_value("width")
        height = structure.get_value("height")
        if not width or not height or len(data) < height * width * 4:
            return None
        stride = len(data) // height  # nvvidconv may pad rows
        rows = np.frombuffer(data, dtype=np.uint8).reshape(height, stride)
        # Explicit copy: frombuffer views are read-only, and the caller draws
        # onto this array in place.
        return rows[:, : width * 4].reshape(height, width, 4).copy()

    def encode_bgrx(self, key: str, image: np.ndarray, quality: int = 90) -> Optional[bytes]:
        if image.ndim != 3 or image.shape[2] != 4:
            return None
        height, width = image.shape[:2]
        return self._encode(key, np.ascontiguousarray(image).tobytes(), "BGRx", width, height, quality)

    # ── internals ───────────────────────────────────────────────────────────

    def _encode(self, key: str, data: bytes, fmt: str, width: int, height: int, quality: int) -> Optional[bytes]:
        pipe_key = ("enc", key, fmt, width, height)
        if fmt == "GRAY8":
            # nvjpegenc takes system-memory GRAY8 directly.
            convert = ""
        else:
            # NV12/BGRx go through nvvidconv into NVMM; color conversion
            # (for BGRx) happens in hardware on the way.
            convert = "nvvidconv ! video/x-raw(memory:NVMM),format=I420 ! "
        description = (
            f"appsrc name=src is-live=true format=time do-timestamp=true "
            f"caps=video/x-raw,format={fmt},width={width},height={height},framerate=0/1 ! "
            f"{convert}nvjpegenc quality={int(quality)} ! appsink name=sink sync=false max-buffers=2 drop=true"
        )
        result = self._run(pipe_key, lambda: _Pipeline(description, "jpeg"), data)
        return None if result is None else result[0]

    def _run(self, pipe_key: Tuple, factory, data: bytes) -> Optional[Tuple[bytes, object]]:
        if self._failures.get(pipe_key, 0) >= _MAX_KEY_FAILURES:
            return None
        try:
            with self._registry_lock:
                pipeline = self._pipelines.get(pipe_key)
                if pipeline is None:
                    pipeline = factory()
                    self._pipelines[pipe_key] = pipeline
            result = pipeline.process(data)
        except Exception as exc:
            self._record_failure(pipe_key, f"error: {exc}")
            return None
        if result is None:
            self._record_failure(pipe_key, "no output buffer")
            return None
        self._failures.pop(pipe_key, None)
        return result

    def _record_failure(self, pipe_key: Tuple, reason: str) -> None:
        with self._registry_lock:
            pipeline = self._pipelines.pop(pipe_key, None)
            count = self._failures.get(pipe_key, 0) + 1
            self._failures[pipe_key] = count
        if pipeline is not None:
            pipeline.destroy()
        if count >= _MAX_KEY_FAILURES:
            self._log(f"hw_jpeg: {pipe_key} failed {count}x ({reason}); disabled, CPU fallback from now on")
        else:
            self._log(f"hw_jpeg: {pipe_key} failed ({reason}); pipeline rebuilt on next frame")

    def status(self) -> Dict[str, object]:
        with self._registry_lock:
            return {
                "active_pipelines": [str(k) for k in self._pipelines],
                "disabled": [str(k) for k, v in self._failures.items() if v >= _MAX_KEY_FAILURES],
            }
