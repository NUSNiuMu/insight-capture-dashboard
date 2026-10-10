"""Stateful H.264 decoding with source timestamps retained on output frames."""

import numpy as np

from insight_capture.media.h264 import H264Gate


class H264Decoder:
    def __init__(self, hardware=True):
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        self.Gst = Gst
        Gst.init(None)
        self.gate = H264Gate()
        decoder = "nvv4l2decoder ! nvvidconv ! video/x-raw,format=BGRx ! " if hardware else "avdec_h264 ! "
        self.pipeline = Gst.parse_launch(
            "appsrc name=source format=time is-live=false block=false "
            "caps=video/x-h264,stream-format=byte-stream,alignment=au ! "
            "h264parse ! " + decoder +
            "videoconvert ! video/x-raw,format=BGR ! appsink name=sink sync=false max-buffers=16 drop=false"
        )
        self.source = self.pipeline.get_by_name("source")
        self.sink = self.pipeline.get_by_name("sink")
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

    def pull(self, timeout_ns=0):
        sample = self.sink.emit("try-pull-sample", int(timeout_ns))
        if sample is None:
            error = self.pipeline.get_bus().pop_filtered(self.Gst.MessageType.ERROR)
            if error is not None:
                raise RuntimeError(str(error.parse_error()))
            return None
        caps = sample.get_caps().get_structure(0)
        width, height = caps.get_value("width"), caps.get_value("height")
        buffer = sample.get_buffer()
        ok, info = buffer.map(self.Gst.MapFlags.READ)
        if not ok:
            raise RuntimeError("Cannot map decoded H.264 frame")
        try:
            # GStreamer aligns packed BGR rows to four bytes.
            stride = (width * 3 + 3) & ~3
            pixels = np.frombuffer(info.data, np.uint8).reshape(height, stride)
            frame = pixels[:, :width * 3].reshape(height, width, 3).copy()
            return int(buffer.pts), frame
        finally:
            buffer.unmap(info)

    def finish(self):
        self.source.emit("end-of-stream")

    def close(self):
        self.pipeline.set_state(self.Gst.State.NULL)
