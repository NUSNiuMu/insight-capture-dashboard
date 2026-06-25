#!/usr/bin/env python3

import argparse
import sys

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstSdp", "1.0")
gi.require_version("GstWebRTC", "1.0")
from gi.repository import GLib, Gst, GstWebRTC  # noqa: E402


def build_pipeline_description(codec: str, width: int, height: int, fps: int) -> str:
    caps = f"video/x-raw,width={width},height={height},framerate={fps}/1"
    if codec == "vp8":
        encoder = "vp8enc deadline=1 keyframe-max-dist=30 target-bitrate=800000"
        payloader = "rtpvp8pay pt=96"
        rtp_caps = "application/x-rtp,media=video,encoding-name=VP8,payload=96"
    elif codec == "h264":
        encoder = "openh264enc bitrate=800000 gop-size=30"
        payloader = "h264parse config-interval=-1 ! rtph264pay pt=96 config-interval=-1"
        rtp_caps = "application/x-rtp,media=video,encoding-name=H264,payload=96"
    else:
        raise ValueError(f"Unsupported codec: {codec}")
    return (
        "videotestsrc is-live=true pattern=ball "
        f"! {caps} "
        "! videoconvert "
        "! queue max-size-buffers=2 leaky=downstream "
        f"! {encoder} "
        f"! {payloader} "
        f"! {rtp_caps} "
        "! webrtcbin name=webrtc bundle-policy=max-bundle"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe local GStreamer WebRTC offer creation.")
    parser.add_argument("--codec", choices=("vp8", "h264"), default="vp8")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=360)
    parser.add_argument("--fps", type=int, default=15)
    args = parser.parse_args()

    Gst.init(None)
    loop = GLib.MainLoop()
    pipeline = Gst.parse_launch(build_pipeline_description(args.codec, args.width, args.height, args.fps))
    webrtc = pipeline.get_by_name("webrtc")
    if webrtc is None:
        raise RuntimeError("webrtcbin was not created")

    state = {"done": False, "sdp": None, "error": None}

    def finish() -> bool:
        if loop.is_running():
            loop.quit()
        return False

    def on_offer_created(promise, _unused) -> None:
        reply = promise.get_reply()
        offer = reply.get_value("offer") if reply is not None else None
        if offer is None:
            state["error"] = "create-offer returned no offer"
            GLib.idle_add(finish)
            return
        local_promise = Gst.Promise.new()
        webrtc.emit("set-local-description", offer, local_promise)
        local_promise.interrupt()
        state["sdp"] = offer.sdp.as_text()
        state["done"] = True
        GLib.idle_add(finish)

    def on_negotiation_needed(element) -> None:
        promise = Gst.Promise.new_with_change_func(on_offer_created, None)
        element.emit("create-offer", None, promise)

    def on_bus_message(_bus, message) -> None:
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            state["error"] = f"{error.message} | {debug or ''}"
            GLib.idle_add(finish)

    webrtc.connect("on-negotiation-needed", on_negotiation_needed)
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", on_bus_message)

    pipeline.set_state(Gst.State.PLAYING)
    GLib.timeout_add_seconds(5, finish)
    loop.run()
    pipeline.set_state(Gst.State.NULL)

    if state["error"]:
        print(f"WebRTC probe failed: {state['error']}", file=sys.stderr)
        return 1
    if not state["sdp"]:
        print("WebRTC probe timed out before creating an SDP offer", file=sys.stderr)
        return 1
    print(f"WebRTC probe ok: codec={args.codec} sdp_bytes={len(state['sdp'])}")
    first_lines = "\n".join(state["sdp"].splitlines()[:8])
    print(first_lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
