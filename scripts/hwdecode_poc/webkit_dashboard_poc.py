#!/usr/bin/env python3

"""Run the dashboard in WebKitGTK with visible WebRTC decode diagnostics."""

from __future__ import annotations

import json
import signal
import sys
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")

from gi.repository import Gtk, WebKit2  # noqa: E402


DIAGNOSTICS_SCRIPT = r"""
(() => {
  if (window.__hwdecodePocInstalled) return;
  window.__hwdecodePocInstalled = true;

  const errors = [];
  window.addEventListener("error", (event) => {
    errors.push(String(event.error || event.message || "unknown error"));
  });
  window.addEventListener("unhandledrejection", (event) => {
    errors.push(String(event.reason || "unhandled rejection"));
  });

  const peers = [];
  const NativePeerConnection = window.RTCPeerConnection;
  if (NativePeerConnection) {
    function TrackedPeerConnection(...args) {
      const peer = new NativePeerConnection(...args);
      peers.push(peer);
      return peer;
    }
    TrackedPeerConnection.prototype = NativePeerConnection.prototype;
    Object.setPrototypeOf(TrackedPeerConnection, NativePeerConnection);
    window.RTCPeerConnection = TrackedPeerConnection;
  }

  const rafSamples = [];
  function sampleRaf(now) {
    rafSamples.push(now);
    while (rafSamples.length && rafSamples[0] < now - 2000) rafSamples.shift();
    requestAnimationFrame(sampleRaf);
  }
  requestAnimationFrame(sampleRaf);

  function webglInfo() {
    const canvas = document.createElement("canvas");
    const gl = canvas.getContext("webgl2") || canvas.getContext("webgl");
    if (!gl) return { available: false };
    const extension = gl.getExtension("WEBGL_debug_renderer_info");
    return {
      available: true,
      renderer: extension
        ? gl.getParameter(extension.UNMASKED_RENDERER_WEBGL)
        : gl.getParameter(gl.RENDERER),
      vendor: extension
        ? gl.getParameter(extension.UNMASKED_VENDOR_WEBGL)
        : gl.getParameter(gl.VENDOR),
    };
  }

  async function snapshot() {
    const inbound = [];
    for (const peer of peers) {
      const reports = await peer.getStats();
      reports.forEach((report) => {
        if (report.type !== "inbound-rtp" || report.kind !== "video") return;
        inbound.push({
          framesDecoded: report.framesDecoded,
          framesDropped: report.framesDropped,
          framesPerSecond: report.framesPerSecond,
          totalDecodeTime: report.totalDecodeTime,
          jitterBufferDelay: report.jitterBufferDelay,
          jitterBufferEmittedCount: report.jitterBufferEmittedCount,
          frameWidth: report.frameWidth,
          frameHeight: report.frameHeight,
          codecId: report.codecId,
        });
      });
    }

    const videos = Array.from(document.querySelectorAll("video")).map((video) => {
      const quality = video.getVideoPlaybackQuality
        ? video.getVideoPlaybackQuality()
        : {};
      return {
        readyState: video.readyState,
        paused: video.paused,
        width: video.videoWidth,
        height: video.videoHeight,
        currentTime: video.currentTime,
        totalVideoFrames: quality.totalVideoFrames,
        droppedVideoFrames: quality.droppedVideoFrames,
      };
    });

    const duration = rafSamples.length > 1
      ? (rafSamples[rafSamples.length - 1] - rafSamples[0]) / 1000
      : 0;
    return {
      timestamp: new Date().toISOString(),
      userAgent: navigator.userAgent,
      capabilities: {
        peerConnection: typeof window.RTCPeerConnection,
        mediaDevices: typeof navigator.mediaDevices,
        webSocket: typeof window.WebSocket,
        videoCodecs: window.RTCRtpReceiver?.getCapabilities?.("video")
          ?.codecs?.map((codec) => codec.mimeType) || [],
      },
      errors: errors.slice(-10),
      peerCount: peers.length,
      rafFps: duration > 0 ? (rafSamples.length - 1) / duration : 0,
      videos,
      inbound,
      webgl: webglInfo(),
    };
  }

  function ensurePanel() {
    let panel = document.getElementById("hwdecode-poc-panel");
    if (panel) return panel;
    panel = document.createElement("pre");
    panel.id = "hwdecode-poc-panel";
    Object.assign(panel.style, {
      position: "fixed",
      left: "8px",
      bottom: "8px",
      zIndex: "2147483647",
      maxWidth: "48vw",
      maxHeight: "45vh",
      overflow: "auto",
      margin: "0",
      padding: "10px",
      color: "#d7ffe1",
      background: "rgba(0, 0, 0, 0.82)",
      border: "1px solid #4ade80",
      borderRadius: "6px",
      font: "11px/1.35 monospace",
      pointerEvents: "none",
      whiteSpace: "pre-wrap",
    });
    document.documentElement.appendChild(panel);
    return panel;
  }

  setInterval(async () => {
    try {
      const data = await snapshot();
      ensurePanel().textContent = JSON.stringify(data, null, 2);
      window.webkit?.messageHandlers?.hwdecode?.postMessage(
        JSON.stringify(data)
      );
    } catch (error) {
      console.error("hwdecode PoC metrics failed", error);
    }
  }, 2000);
})();
"""


class PocWindow:
    def __init__(self, url: str) -> None:
        self.started_at = time.monotonic()
        manager = WebKit2.UserContentManager()
        manager.register_script_message_handler("hwdecode")
        manager.connect(
            "script-message-received::hwdecode", self._on_diagnostics
        )
        manager.add_script(
            WebKit2.UserScript.new(
                DIAGNOSTICS_SCRIPT,
                WebKit2.UserContentInjectedFrames.TOP_FRAME,
                WebKit2.UserScriptInjectionTime.START,
                None,
                None,
            )
        )

        self.window = Gtk.Window(title="Insight WebKitGTK hardware decode PoC")
        self.window.set_default_size(1920, 1080)
        self.window.connect("destroy", Gtk.main_quit)

        self.view = WebKit2.WebView.new_with_user_content_manager(manager)
        settings = self.view.get_settings()
        settings.set_enable_webgl(True)
        settings.set_enable_media(True)
        settings.set_enable_media_stream(True)
        settings.set_enable_webrtc(True)
        settings.set_enable_media_capabilities(True)
        settings.set_media_playback_requires_user_gesture(False)
        settings.set_media_playback_allows_inline(True)

        self.view.connect("load-changed", self._on_load_changed)
        self.view.connect("load-failed", self._on_load_failed)
        self.window.add(self.view)
        self.window.show_all()
        self.view.load_uri(url)

    def _on_diagnostics(
        self, _manager: WebKit2.UserContentManager, result
    ) -> None:
        value = result.get_js_value()
        payload = value.to_string()
        try:
            decoded = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            print(f"METRICS {payload}", flush=True)
            return
        print(f"METRICS {json.dumps(decoded, sort_keys=True)}", flush=True)

    def _on_load_changed(self, view: WebKit2.WebView, event) -> None:
        print(
            f"LOAD event={event.value_name} uri={view.get_uri()} "
            f"elapsed={time.monotonic() - self.started_at:.3f}s",
            flush=True,
        )

    def _on_load_failed(
        self, _view: WebKit2.WebView, event, uri: str, error
    ) -> bool:
        print(
            f"LOAD_FAILED event={event.value_name} uri={uri} error={error}",
            file=sys.stderr,
            flush=True,
        )
        return False


def main() -> int:
    url = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "http://127.0.0.1:8765/3d?hwdecode_poc=1"
    )
    signal.signal(signal.SIGINT, lambda *_args: Gtk.main_quit())
    PocWindow(url)
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
