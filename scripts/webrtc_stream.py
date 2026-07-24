#!/usr/bin/env python3

"""Per-viewer hardware H.264 WebRTC streams with polling fallback."""

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

# Downscale on VIC to match dashboard thumbnail sizes.
STREAM_MAX_WIDTH = 540


def _scaled_dims(width: int, height: int) -> Tuple[int, int]:
    """Cap width at STREAM_MAX_WIDTH keeping aspect, rounded to even (NV12)."""
    if width <= STREAM_MAX_WIDTH:
        return (width, height)
    scale = STREAM_MAX_WIDTH / float(width)
    out_w = STREAM_MAX_WIDTH & ~1
    out_h = int(round(height * scale)) & ~1
    return (out_w, max(2, out_h))


def _bitrate_for(width: int, height: int) -> int:
    return max(_MIN_BITRATE, min(_MAX_BITRATE, int(width * height * 30 * 0.15)))


# One query resolves both of a browser's candidates (UDP + TCP carry the same
# name), and reconnects reuse it too. Entries are tiny; no eviction needed.
_mdns_cache: Dict[str, str] = {}


def _resolve_mdns(hostname: str, timeout: float = 2.0) -> Optional[str]:
    """Resolve browser-obfuscated .local ICE candidates via multicast DNS."""
    import socket
    import struct
    import time

    target = hostname.rstrip(".").lower()
    cached = _mdns_cache.get(target)
    if cached:
        return cached
    query = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0)
    for label in target.split("."):
        encoded = label.encode("utf-8")
        query += struct.pack("B", len(encoded)) + encoded
    query += b"\x00" + struct.pack(">HH", 1, 1)  # A record, class IN

    def _skip_name(data: bytes, pos: int) -> int:
        while pos < len(data) and data[pos] != 0:
            if data[pos] & 0xC0:
                return pos + 2
            pos += 1 + data[pos]
        return pos + 1

    def _read_name(data: bytes, pos: int) -> str:
        parts = []
        hops = 0
        while pos < len(data) and data[pos] != 0 and hops < 16:
            if data[pos] & 0xC0:
                pos = ((data[pos] & 0x3F) << 8) | data[pos + 1]
                hops += 1
                continue
            parts.append(data[pos + 1 : pos + 1 + data[pos]].decode("utf-8", "replace"))
            pos += 1 + data[pos]
        return ".".join(parts).lower()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
        sock.bind(("", 5353))
        sock.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_ADD_MEMBERSHIP,
            socket.inet_aton("224.0.0.251") + socket.inet_aton("0.0.0.0"),
        )
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        sock.sendto(query, ("224.0.0.251", 5353))
        end_time = time.monotonic() + timeout
        while time.monotonic() < end_time:
            sock.settimeout(max(0.05, end_time - time.monotonic()))
            try:
                data, _addr = sock.recvfrom(4096)
            except socket.timeout:
                break
            try:
                flags, qdcount, ancount = struct.unpack(">HHH", data[2:8])
                if not (flags & 0x8000) or not ancount:
                    continue  # a query (possibly our own echo), not a response
                pos = 12
                for _ in range(qdcount):
                    pos = _skip_name(data, pos) + 4
                for _ in range(ancount):
                    name = _read_name(data, pos)
                    pos = _skip_name(data, pos)
                    rtype, _rclass, _ttl, rdlen = struct.unpack(">HHIH", data[pos : pos + 10])
                    pos += 10
                    if rtype == 1 and rdlen == 4 and name == target:
                        address = socket.inet_ntoa(data[pos : pos + 4])
                        _mdns_cache[target] = address
                        return address
                    pos += rdlen
            except (IndexError, struct.error):
                continue
        return None
    except OSError:
        return None
    finally:
        sock.close()


class WebRtcSession:
    """One locked H.264 pipeline for a viewer-camera pair."""

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
        out_width, out_height = _scaled_dims(width, height)
        bitrate = _bitrate_for(out_width, out_height)
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
            f"nvvidconv ! video/x-raw(memory:NVMM),format=NV12,width={out_width},height={out_height} ! "
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
            f"webrtc[{self.camera_name}]: streaming {fmt} {width}x{height} -> {out_width}x{out_height} at {bitrate // 1000} kbps"
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
        if webrtc is None or not candidate:
            return
        # candidate:<f> <comp> <proto> <prio> <addr> <port> typ ... -- addr
        # is field 5. See _resolve_mdns for why .local names must be
        # rewritten to real IPs before webrtcbin/libnice sees them.
        parts = candidate.split()
        if len(parts) > 4 and parts[4].endswith(".local"):
            # Resolve mDNS off the event loop so signaling remains responsive.
            threading.Thread(
                target=self._resolve_and_add,
                args=(int(mline_index), parts),
                daemon=True,
                name=f"mdns_resolve_{self.camera_name}",
            ).start()
            return
        webrtc.emit("add-ice-candidate", int(mline_index), candidate)

    def _resolve_and_add(self, mline_index: int, parts: List[str]) -> None:
        hostname = parts[4]
        resolved = None
        # WiFi multicast is lossy; a couple of short attempts beats one
        # long one (the responder answers in ~10ms when the query gets
        # through at all).
        for _attempt in range(3):
            resolved = _resolve_mdns(hostname, timeout=0.7)
            if resolved:
                break
        if not resolved:
            self._log(
                f"webrtc[{self.camera_name}]: cannot resolve mDNS candidate "
                f"{hostname} (multicast blocked?); dropping it"
            )
            return
        self._log(f"webrtc[{self.camera_name}]: mDNS {hostname} -> {resolved}")
        parts[4] = resolved
        with self._lock:
            webrtc = self._webrtc
        if webrtc is not None:
            webrtc.emit("add-ice-candidate", mline_index, " ".join(parts))

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
    """Registry of worker-process WebRTC sessions keyed by camera."""

    def __init__(
        self,
        log: Callable[[str], None],
        on_session_state_change: Optional[Callable[[str, bool], None]] = None,
    ) -> None:
        self._log = log
        self._on_session_state_change = on_session_state_change
        self._lock = threading.Lock()
        self._sessions: Dict[str, List[WebRtcSession]] = {}
        # webrtcbin's ICE/DTLS internals expect a running GLib main loop for
        # their timers; hw_jpeg's pull-based pipelines never needed one, so
        # this module owns it.
        self._main_loop = GLib.MainLoop()
        threading.Thread(target=self._main_loop.run, daemon=True, name="webrtc_glib_loop").start()

    @classmethod
    def create(cls, log=print, on_session_state_change=None) -> Optional["WebRtcStreams"]:
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
        return cls(log=log, on_session_state_change=on_session_state_change)

    def create_session(
        self, camera_name: str, topic_type: str, send_signal: Callable[[Dict], None]
    ) -> WebRtcSession:
        session = WebRtcSession(camera_name, topic_type, send_signal, self._log)
        with self._lock:
            sessions = self._sessions.setdefault(camera_name, [])
            was_empty = not sessions
            sessions.append(session)
        if was_empty and self._on_session_state_change:
            self._on_session_state_change(camera_name, True)
        return session

    def close_session(self, session: WebRtcSession) -> None:
        with self._lock:
            sessions = self._sessions.get(session.camera_name, [])
            if session in sessions:
                sessions.remove(session)
            now_empty = not sessions
        session.close()
        if now_empty and self._on_session_state_change:
            self._on_session_state_change(session.camera_name, False)

    def has_sessions(self, camera_name: str) -> bool:
        sessions = self._sessions.get(camera_name)
        return bool(sessions)

    def push_resolved_frame(self, camera_name: str, fmt: str, width: int, height: int, data: bytes) -> None:
        """Fan a resolved frame out to all sessions for its camera."""
        with self._lock:
            sessions = list(self._sessions.get(camera_name, ()))
        if not sessions:
            return
        for session in sessions:
            session.push_frame(data, fmt, width, height)
