#!/usr/bin/env python3

"""Per-viewer WebRTC camera streams (nvv4l2h264enc + webrtcbin).

The polling display path (JSON poll + per-frame HTTP GET + browser JPEG
decode) caps the UI at the poll rate and jitters under load. This module
streams H.264 instead: raw NV12/GRAY8 frames go appsrc -> nvvidconv ->
nvv4l2h264enc (hardware) -> rtph264pay -> webrtcbin, and compressed
(JPEG passthrough) cameras decode on the NVJPEG engine first. The browser
decodes H.264 natively -- usually in hardware -- so both ends of the wire
are accelerated.

One session per (viewer, camera): sessions are cheap (the encoder is the
scarce resource, and NVENC handles a handful of 1080p sessions), and
per-viewer encoders sidestep keyframe/bitrate coordination between
viewers. Nobody watching means zero cost: push_ros_frame() returns before
touching the message bytes when a camera has no sessions.

Everything degrades to the existing polling path: create() returns None
when elements are missing, and a browser that can't decode H.264 (the
vendored kiosk Chromium has no proprietary codecs) simply never reaches
the 'playing' state, so the frontend keeps polling. A session's pipeline
is built lazily on the first frame because the appsrc caps need the
camera's real format/size.
"""

import threading
from typing import Callable, Dict, List, Optional, Tuple

try:
    import gi

    gi.require_version("Gst", "1.0")
    gi.require_version("GstWebRTC", "1.0")
    gi.require_version("GstSdp", "1.0")
    from gi.repository import GLib, Gst, GstSdp, GstWebRTC
except Exception:  # pragma: no cover - non-Jetson dev machines
    Gst = None

from hw_jpeg import HwJpegCodec

_REQUIRED_ELEMENTS = (
    "webrtcbin",
    "nicesrc",
    "dtlssrtpenc",
    "h264parse",
    "rtph264pay",
    "nvvidconv",
    "nvv4l2h264enc",
    "nvjpegdec",
)

# ~0.15 bits per pixel per frame at 30fps: 544x640 -> ~1.6 Mbps,
# 1088x1920 -> ~9.4 Mbps. Clamped so tiny streams still get enough for
# clean motion and huge ones don't swamp WiFi viewers.
_MIN_BITRATE = 1_500_000
_MAX_BITRATE = 10_000_000


def _bitrate_for(width: int, height: int) -> int:
    return max(_MIN_BITRATE, min(_MAX_BITRATE, int(width * height * 30 * 0.15)))


class WebRtcSession:
    """One viewer's H.264 stream for one camera.

    Signaling flows through send_signal (thread-safe, provided by the
    websocket handler); frames arrive on the camera's worker thread;
    webrtcbin callbacks arrive on GStreamer threads. self._lock guards
    pipeline construction/teardown across all three.
    """

    def __init__(
        self,
        camera_name: str,
        topic_type: str,
        send_signal: Callable[[Dict], None],
        log: Callable[[str], None],
    ) -> None:
        self.camera_name = camera_name
        self.topic_type = topic_type
        self._send_signal = send_signal
        self._log = log
        self._lock = threading.Lock()
        self._pipeline = None
        self._appsrc = None
        self._webrtc = None
        self._caps_key: Optional[Tuple] = None
        self._closed = False

    # ── frame ingest (camera worker thread) ─────────────────────────────────

    def push_frame(self, data: bytes, fmt: str, width: int, height: int) -> None:
        caps_key = (fmt, width, height)
        with self._lock:
            if self._closed:
                return
            if self._pipeline is None:
                try:
                    self._build_pipeline(fmt, width, height)
                    self._caps_key = caps_key
                except Exception as exc:
                    self._log(f"webrtc[{self.camera_name}]: pipeline build failed ({exc}); closing session")
                    self._teardown_locked()
                    return
            elif self._caps_key != caps_key:
                # Camera format changed mid-session (e.g. playback of a
                # different bag). Renegotiating is not worth the complexity;
                # the viewer reconnects and gets a fresh session.
                return
            appsrc = self._appsrc
        buf = Gst.Buffer.new_wrapped(data)
        if appsrc.emit("push-buffer", buf) != Gst.FlowReturn.OK:
            self._log(f"webrtc[{self.camera_name}]: push-buffer failed; closing session")
            self.close()

    # ── pipeline (called under self._lock) ──────────────────────────────────

    def _build_pipeline(self, fmt: str, width: int, height: int) -> None:
        bitrate = _bitrate_for(width, height)
        if fmt == "JPEG":
            source = (
                f"appsrc name=src is-live=true format=time do-timestamp=true "
                f"block=false max-buffers=3 leaky-type=downstream "
                f"caps=image/jpeg,width={width},height={height},framerate=30/1 ! "
                f"nvjpegdec ! "
            )
        else:
            source = (
                f"appsrc name=src is-live=true format=time do-timestamp=true "
                f"block=false max-buffers=3 leaky-type=downstream "
                f"caps=video/x-raw,format={fmt},width={width},height={height},framerate=30/1 ! "
            )
        # insert-sps-pps + config-interval=-1: parameter sets ride along with
        # every IDR so a viewer joining mid-stream can start decoding at the
        # next keyframe. idrinterval=30 bounds that wait to ~1s.
        description = (
            f"{source}"
            f"nvvidconv ! video/x-raw(memory:NVMM),format=NV12 ! "
            f"nvv4l2h264enc bitrate={bitrate} insert-sps-pps=true idrinterval=30 "
            f"iframeinterval=30 maxperf-enable=true ! "
            f"h264parse config-interval=-1 ! "
            f"rtph264pay pt=96 mtu=1200 aggregate-mode=zero-latency config-interval=-1 ! "
            f"application/x-rtp,media=video,encoding-name=H264,payload=96 ! "
            f"webrtcbin name=webrtc bundle-policy=max-bundle"
        )
        pipeline = Gst.parse_launch(description)
        webrtc = pipeline.get_by_name("webrtc")
        webrtc.connect("on-negotiation-needed", self._on_negotiation_needed)
        webrtc.connect("on-ice-candidate", self._on_ice_candidate)
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            pipeline.set_state(Gst.State.NULL)
            raise RuntimeError("pipeline refused to start")
        self._pipeline = pipeline
        self._appsrc = pipeline.get_by_name("src")
        self._webrtc = webrtc
        self._log(
            f"webrtc[{self.camera_name}]: streaming {fmt} {width}x{height} at {bitrate // 1000} kbps"
        )

    # ── webrtcbin callbacks (GStreamer threads) ─────────────────────────────

    def _on_negotiation_needed(self, webrtc) -> None:
        try:
            # We only ever send; advertising sendonly keeps the browser from
            # allocating a return channel it will never use.
            transceiver = webrtc.emit("get-transceiver", 0)
            if transceiver is not None:
                transceiver.set_property(
                    "direction", GstWebRTC.WebRTCRTPTransceiverDirection.SENDONLY
                )
        except Exception:
            pass
        promise = Gst.Promise.new_with_change_func(self._on_offer_created, webrtc)
        webrtc.emit("create-offer", None, promise)

    def _on_offer_created(self, promise, webrtc) -> None:
        reply = promise.get_reply()
        offer = reply.get_value("offer")
        webrtc.emit("set-local-description", offer, Gst.Promise.new())
        self._send_signal({"type": "offer", "sdp": offer.sdp.as_text()})

    def _on_ice_candidate(self, _webrtc, mline_index: int, candidate: str) -> None:
        self._send_signal({"type": "ice", "candidate": candidate, "sdpMLineIndex": int(mline_index)})

    # ── signaling from the browser (websocket thread) ───────────────────────

    def set_remote_answer(self, sdp_text: str) -> None:
        with self._lock:
            webrtc = self._webrtc
        if webrtc is None or not sdp_text:
            return
        ok, sdp_message = GstSdp.SDPMessage.new()
        if ok != GstSdp.SDPResult.OK:
            return
        if GstSdp.sdp_message_parse_buffer(sdp_text.encode(), sdp_message) != GstSdp.SDPResult.OK:
            self._log(f"webrtc[{self.camera_name}]: unparseable SDP answer")
            return
        answer = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.ANSWER, sdp_message
        )
        webrtc.emit("set-remote-description", answer, Gst.Promise.new())

    def add_ice_candidate(self, mline_index: int, candidate: str) -> None:
        with self._lock:
            webrtc = self._webrtc
        if webrtc is not None and candidate:
            webrtc.emit("add-ice-candidate", int(mline_index), candidate)

    # ── teardown ─────────────────────────────────────────────────────────────

    def close(self) -> None:
        with self._lock:
            self._teardown_locked()

    def _teardown_locked(self) -> None:
        self._closed = True
        pipeline = self._pipeline
        self._pipeline = None
        self._appsrc = None
        self._webrtc = None
        if pipeline is not None:
            try:
                pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass


class WebRtcStreams:
    """Registry of live sessions, keyed by camera. Use create()."""

    def __init__(self, log: Callable[[str], None]) -> None:
        self._log = log
        self._lock = threading.Lock()
        self._sessions: Dict[str, List[WebRtcSession]] = {}
        # webrtcbin's ICE/DTLS internals expect a running GLib main loop for
        # their timers; hw_jpeg's pull-based pipelines never needed one, so
        # this module owns it.
        self._main_loop = GLib.MainLoop()
        threading.Thread(target=self._main_loop.run, daemon=True, name="webrtc_glib_loop").start()

    @classmethod
    def create(cls, log=print) -> Optional["WebRtcStreams"]:
        if Gst is None:
            log("webrtc: PyGObject/GstWebRTC not importable; WebRTC streaming disabled")
            return None
        try:
            if not Gst.is_initialized():
                Gst.init(None)
        except Exception as exc:
            log(f"webrtc: Gst.init failed ({exc}); WebRTC streaming disabled")
            return None
        missing = [name for name in _REQUIRED_ELEMENTS if Gst.ElementFactory.find(name) is None]
        if missing:
            log(f"webrtc: missing GStreamer elements {missing}; WebRTC streaming disabled")
            return None
        log("webrtc: hardware H.264 WebRTC streaming enabled")
        return cls(log=log)

    def create_session(
        self, camera_name: str, topic_type: str, send_signal: Callable[[Dict], None]
    ) -> WebRtcSession:
        session = WebRtcSession(camera_name, topic_type, send_signal, self._log)
        with self._lock:
            self._sessions.setdefault(camera_name, []).append(session)
        return session

    def close_session(self, session: WebRtcSession) -> None:
        with self._lock:
            sessions = self._sessions.get(session.camera_name, [])
            if session in sessions:
                sessions.remove(session)
        session.close()

    def has_sessions(self, camera_name: str) -> bool:
        sessions = self._sessions.get(camera_name)
        return bool(sessions)

    def push_ros_frame(self, camera_name: str, topic_type: str, msg, frame) -> None:
        """Fan one frame out to every session on this camera.

        Called on the camera's worker thread for every frame; must cost
        nothing when nobody is watching. `frame` is the CameraFrame the
        polling path already produced -- compressed cameras reuse its JPEG
        bytes (which include the hand overlay when enabled) while raw
        cameras feed the untouched NV12/GRAY8 message bytes.
        """
        with self._lock:
            sessions = list(self._sessions.get(camera_name, ()))
        if not sessions:
            return
        if topic_type == "compressed":
            if frame is None or frame.width <= 0 or frame.height <= 0:
                return
            data, fmt, width, height = frame.data, "JPEG", frame.width, frame.height
        else:
            layout = HwJpegCodec.ros_image_layout(msg)
            if layout is None:
                return
            fmt, width, height = layout
            data = bytes(msg.data)
        for session in sessions:
            session.push_frame(data, fmt, width, height)
