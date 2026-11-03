"""Persistent Jetson NVJPEG hardware encoding pipelines."""

import threading
from typing import Dict, Optional, Tuple


try:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
except Exception:  # pragma: no cover - non-Jetson dev machines
    Gst = None

_REQUIRED_ELEMENTS = ("nvjpegenc", "nvvidconv")
_PULL_TIMEOUT_NS = int(1e9)


class _Pipeline:
    """One locked, persistent appsrc-to-appsink pipeline."""

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
    """Keyed registry of hardware JPEG encoding pipelines. Use create()."""

    def __init__(self) -> None:
        self._pipelines: Dict[Tuple, _Pipeline] = {}
        self._registry_lock = threading.Lock()

    @classmethod
    def create(cls, log=print) -> Optional["HwJpegCodec"]:
        if Gst is None:
            log("hw_jpeg: PyGObject/GStreamer not importable; JPEG encoding unavailable")
            return None
        try:
            if not Gst.is_initialized():
                Gst.init(None)
        except Exception as exc:
            log(f"hw_jpeg: Gst.init failed ({exc}); JPEG encoding unavailable")
            return None
        missing = [name for name in _REQUIRED_ELEMENTS if Gst.ElementFactory.find(name) is None]
        if missing:
            log(f"hw_jpeg: missing GStreamer elements {missing}; JPEG encoding unavailable")
            return None
        log("hw_jpeg: NVJPEG hardware JPEG encoding enabled")
        return cls()

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
            # Derive NV12 luma height because some drivers report total rows.
            total_rows, remainder = divmod(data_len, width)
            if remainder == 0 and total_rows > 0 and total_rows % 3 == 0:
                return ("NV12", width, total_rows * 2 // 3)
            return None
        return None

    def encode_ros_image(self, key: str, msg, quality: int = 82) -> Tuple[bytes, int, int]:
        """Encode mono8/NV12 image data, returning JPEG dimensions on success."""
        layout = self.ros_image_layout(msg)
        if layout is None:
            raise ValueError("Hardware JPEG encoding requires packed mono8 or NV12 image data")
        fmt, width, height = layout
        jpeg = self._encode(key, bytes(msg.data), fmt, width, height, quality)
        return jpeg, width, height

    # ── internals ───────────────────────────────────────────────────────────

    def _encode(self, key: str, data: bytes, fmt: str, width: int, height: int, quality: int) -> bytes:
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
        return result[0]

    def _run(self, pipe_key: Tuple, factory, data: bytes) -> Tuple[bytes, object]:
        try:
            with self._registry_lock:
                pipeline = self._pipelines.get(pipe_key)
                if pipeline is None:
                    pipeline = factory()
                    self._pipelines[pipe_key] = pipeline
            result = pipeline.process(data)
            if result is None:
                raise RuntimeError("no hardware JPEG output buffer")
        except Exception as exc:
            with self._registry_lock:
                pipeline = self._pipelines.pop(pipe_key, None)
            if pipeline is not None:
                pipeline.destroy()
            raise RuntimeError(f"Hardware JPEG encoding failed for {pipe_key}: {exc}") from exc
        return result

    def status(self) -> Dict[str, object]:
        with self._registry_lock:
            return {
                "active_pipelines": [str(k) for k in self._pipelines],
            }
