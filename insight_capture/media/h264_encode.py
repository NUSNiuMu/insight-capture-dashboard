"""Hardware H.264 encoding with one source timestamp per access unit."""


class H264Encoder:
    def __init__(self, width, height, bitrate):
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
        self.Gst = Gst
        self.shape = (width, height)
        self.last_stamp = None
        self.pipeline = Gst.parse_launch(
            "appsrc name=source format=time is-live=true block=true max-buffers=4 "
            f"caps=video/x-raw,format=NV12,width={width},height={height},framerate=30/1 ! "
            "nvvidconv ! video/x-raw(memory:NVMM),format=NV12 ! "
            f"nvv4l2h264enc name=encoder bitrate={bitrate} profile=0 num-B-Frames=0 "
            "insert-sps-pps=true iframeinterval=30 idrinterval=30 "
            "maxperf-enable=true copy-timestamp=true ! "
            "h264parse config-interval=-1 ! "
            "video/x-h264,stream-format=byte-stream,alignment=au ! "
            "appsink name=sink sync=false max-buffers=8 drop=false"
        )
        self.source = self.pipeline.get_by_name("source")
        self.sink = self.pipeline.get_by_name("sink")
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.close()
            raise RuntimeError("H.264 hardware encoder failed to start")
        # Allocate NVENC/VIC before source subscriptions can fill their input queues.
        self.push(bytes([128]) * (width * height * 3 // 2), 0)
        if self.pull(5 * Gst.SECOND) is None:
            self.close()
            raise RuntimeError("H.264 hardware encoder warm-up timed out")
        self.request_idr()

    def push(self, data, stamp_ns):
        if self.last_stamp is not None and stamp_ns <= self.last_stamp:
            raise ValueError(f"Encoder source timestamps are not strictly increasing: {self.last_stamp} -> {stamp_ns}")
        if len(data) != self.shape[0] * self.shape[1] * 3 // 2:
            raise ValueError("Encoder input is not packed NV12")
        self.last_stamp = stamp_ns
        buffer = self.Gst.Buffer.new_wrapped(bytes(data))
        buffer.pts = stamp_ns
        buffer.dts = self.Gst.CLOCK_TIME_NONE
        buffer.duration = self.Gst.SECOND // 30
        if self.source.emit("push-buffer", buffer) != self.Gst.FlowReturn.OK:
            raise RuntimeError("Hardware encoder rejected a source frame")

    def pull(self, timeout_ns=0):
        sample = self.sink.emit("try-pull-sample", int(timeout_ns))
        if sample is None:
            error = self.pipeline.get_bus().pop_filtered(self.Gst.MessageType.ERROR)
            if error is not None:
                raise RuntimeError(str(error.parse_error()))
            return None
        buffer = sample.get_buffer()
        return int(buffer.pts), buffer.extract_dup(0, buffer.get_size())

    def finish(self):
        if self.source.emit("end-of-stream") != self.Gst.FlowReturn.OK:
            raise RuntimeError("Hardware encoder rejected EOS")

    def request_idr(self):
        self.pipeline.get_by_name("encoder").emit("force-IDR")

    def close(self):
        self.pipeline.set_state(self.Gst.State.NULL)
