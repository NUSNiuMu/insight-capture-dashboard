# Jetson browser hardware-decode PoC

This directory contains isolated diagnostics for evaluating an on-device
browser without changing the production kiosk backend.

## 1. Check Firefox's V4L2-DRM prerequisites

Run inside the dashboard container:

```bash
scripts/hwdecode_poc/probe_firefox_v4l2.sh
```

The probe distinguishes these separate requirements:

- Firefox was built with its V4L2-DRM FFmpeg path.
- A standard `/dev/video*` decoder node is visible.
- Firefox's bundled `v4l2test` accepts that node.
- A running Firefox/RDD process has opened a decoder device.

Firefox hardware decode is not considered available unless all four checks
pass while an H.264 WebRTC stream is playing.

## 2. Install and launch the WebKitGTK PoC

The Jammy updates repository currently provides WebKitGTK 2.50.4:

```bash
scripts/hwdecode_poc/install_webkitgtk_poc.sh
scripts/hwdecode_poc/run_webkitgtk_poc.sh
```

The launcher raises `nvv4l2decoder` to the highest GStreamer rank and disables
the `avdec_h264` and `openh264dec` software decoders. It also injects an
on-page diagnostics panel with:

- decoded and dropped video frame counts;
- inbound WebRTC FPS and decode time;
- `requestAnimationFrame` FPS;
- WebGL renderer information.

Logs default to `/tmp/insight-webkit-hwdecode.log` and GStreamer DOT files to
`/tmp/insight-webkit-gst-dots/`.

If the distribution WebKit build has no `RTCPeerConnection`, its media and
compositor path can still be isolated with a generated H.264 clip:

```bash
gst-launch-1.0 -e videotestsrc num-buffers=300 pattern=ball \
  ! video/x-raw,width=640,height=480,framerate=30/1 \
  ! nvvidconv ! 'video/x-raw(memory:NVMM),format=NV12' \
  ! nvv4l2h264enc insert-sps-pps=true \
  ! h264parse ! mp4mux ! filesink location=/tmp/hwdecode-test.mp4
python3 -m http.server 8877 --directory /tmp
```

Copy `video_decode_test.html` beside the generated clip and launch the PoC
against `http://127.0.0.1:8877/video_decode_test.html`.

See [RESULTS.md](RESULTS.md) for the measurements and failure boundaries from
the Jetson NX test on 2026-07-24.

Seeing `nvv4l2decoder` in a log or DOT graph only proves hardware decode
selection. A zero-copy result additionally requires inspecting the negotiated
buffers at the WebKit sink for DMA-BUF/GLMemory import, plane metadata,
modifier, stride, and the absence of a system-memory conversion/upload path.
