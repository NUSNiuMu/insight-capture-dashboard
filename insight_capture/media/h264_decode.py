"""Stateful H.264 decoding with source timestamps retained on output frames."""

from collections import deque
import threading

import numpy as np

from insight_capture.media.h264 import H264Gate


class H264Decoder:
    def __init__(self, hardware=True, pixel_format="BGR", low_latency=False, frame_selector=None):
        if pixel_format not in {"BGR", "GRAY8"}:
            raise ValueError("Decoder output must be BGR or GRAY8")
        self.channels = 3 if pixel_format == "BGR" else 1
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        self.Gst = Gst
        Gst.init(None)
        self.gate = H264Gate()
        self._discarded = deque(maxlen=256)
        self._selection_lock = threading.Lock()
        self._selection_error = None
        self.selected_frames = self.decoded_frames = 0
        if hardware:
            # Only verified I/P live sources may bypass DPB; replay keeps normal ordering.
            decoder = "nvv4l2decoder name=decoder " + ("disable-dpb=true enable-max-performance=true " if low_latency else "") + "! nvvidconv ! "
            if pixel_format == "BGR":
                decoder += "video/x-raw,format=BGRx ! videoconvert ! "
        else:
            decoder = "avdec_h264 name=decoder ! videoconvert ! "
        self.pipeline = Gst.parse_launch(
            "appsrc name=source format=time is-live=false block=false "
            "caps=video/x-h264,stream-format=byte-stream,alignment=au ! "
            "h264parse ! " + decoder +
            f"video/x-raw,format={pixel_format} ! appsink name=sink sync=false max-buffers=16 drop=false"
        )
        self.source = self.pipeline.get_by_name("source")
        self.sink = self.pipeline.get_by_name("sink")
        if frame_selector is not None:
            # All encoded reference frames are decoded. Reject unused decoded
            # frames before VIC conversion and transfer to system memory.
            def select_decoded(_pad, info):
                buffer = info.get_buffer()
                stamp = int(buffer.pts)
                try:
                    selected = bool(frame_selector(stamp))
                except Exception as exc:
                    self._selection_error = exc
                    selected = False
                with self._selection_lock:
                    self.decoded_frames += 1
                    self.selected_frames += int(selected)
                    if not selected:
                        self._discarded.append(stamp)
                return Gst.PadProbeReturn.OK if selected else Gst.PadProbeReturn.DROP
            self.pipeline.get_by_name("decoder").get_static_pad("src").add_probe(
                Gst.PadProbeType.BUFFER, select_decoded)
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.close()
            raise RuntimeError("H.264 decoder failed to start")

    def push(self, data, stamp_ns):
        data = self.gate.accept(data)
        if data is None:
            return False
        if self.source.get_property("current-level-buffers") >= 16:
            raise RuntimeError("H.264 decoder input backlog exceeded 16 access units")
        buffer = self.Gst.Buffer.new_wrapped(data)
        buffer.pts = int(stamp_ns)
        buffer.dts = self.Gst.CLOCK_TIME_NONE
        if self.source.emit("push-buffer", buffer) != self.Gst.FlowReturn.OK:
            raise RuntimeError("H.264 decoder rejected an access unit")
        return True

    def pull(self, timeout_ns=0, select_frame=None):
        if self._selection_error is not None:
            raise RuntimeError("Decoded frame selection failed") from self._selection_error
        sample = self.sink.emit("try-pull-sample", int(timeout_ns))
        if sample is None:
            error = self.pipeline.get_bus().pop_filtered(self.Gst.MessageType.ERROR)
            if error is not None:
                raise RuntimeError(str(error.parse_error()))
            return None
        caps = sample.get_caps().get_structure(0)
        width, height = caps.get_value("width"), caps.get_value("height")
        buffer = sample.get_buffer()
        if select_frame is not None and not select_frame(int(buffer.pts)):
            return int(buffer.pts), None
        ok, info = buffer.map(self.Gst.MapFlags.READ)
        if not ok:
            raise RuntimeError("Cannot map decoded H.264 frame")
        try:
            # GStreamer aligns packed rows to four bytes.
            stride = (width * self.channels + 3) & ~3
            pixels = np.frombuffer(info.data, np.uint8).reshape(height, stride)
            shape = (height, width, 3) if self.channels == 3 else (height, width)
            frame = pixels[:, :width * self.channels].reshape(shape).copy()
            return int(buffer.pts), frame
        finally:
            buffer.unmap(info)

    def take_discarded_timestamps(self):
        """Release timestamp metadata for frames rejected before conversion."""
        with self._selection_lock:
            stamps = tuple(self._discarded)
            self._discarded.clear()
        return stamps

    def finish(self):
        self.source.emit("end-of-stream")

    def close(self):
        self.pipeline.set_state(self.Gst.State.NULL)
