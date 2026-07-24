# Jetson NX hardware-decode PoC results

Test date: 2026-07-24

Baseline:

- Ubuntu 22.04 container on Jetson NX
- Firefox 152.0.5 official aarch64 build
- WebKitGTK 2.50.4 from Jammy updates
- GStreamer core 1.20.3 / base plugins 1.20.1
- NVIDIA `nvv4l2decoder` with `video/x-raw(memory:NVMM)` output

## Firefox

Firefox contains its V4L2-DRM FFmpeg decoder code, but this machine exposes no
standard `/dev/video*` decoder node. `/dev/v4l2-nvdec` is NVIDIA's special
entry point and Firefox's bundled `v4l2test` cannot query its capabilities.
A playing Firefox/RDD process did not hold any NVDEC or V4L2 decoder device
file descriptor.

Result: the current Firefox package and Jetson FFmpeg/device interface are not
compatible for hardware decode. Do not invest in preference tuning; a usable
standard V4L2-DRM/DRM_PRIME wrapper would be required.

## Distribution WebKitGTK

The first launch failed because the container had NVIDIA EGL libraries but no
GLVND registration for them. Pointing
`__EGL_VENDOR_LIBRARY_FILENAMES` at
`/usr/lib/aarch64-linux-gnu/tegra-egl/nvidia.json` made WebKitGTK launch and
render WebGL.

The Jammy WebKitGTK build exposes `navigator.mediaDevices` but not
`RTCPeerConnection`, even with media-stream settings enabled. The dashboard
therefore cannot use its current WebRTC transport with this distribution
build.

Result: a WebKit build with WebRTC enabled is required. Installing the Jammy
package alone is not a deployable kiosk solution.

## H.264 decoder and memory negotiation

A local 640x480, 30 FPS H.264 clip isolated the media path:

1. WebKit's `decodebin` selected `nvv4l2decoder`.
2. `nvv4l2decoder` opened `/dev/v4l2-nvdec` successfully.
3. The decoder offered `video/x-raw(memory:NVMM)`.
4. WebKit's downstream caps query returned `EMPTY`.
5. The decoder could not enter `PAUSED`.
6. With software fallback allowed, WebKit selected `openh264dec` and presented
   approximately 30 FPS with zero dropped frames.
7. With both software decoders disabled, playback failed as intended.

Installing `gstreamer1.0-gl` did not change the NVMM rejection. The relevant
caps do not meet directly:

- `nvv4l2decoder`: NVMM only
- `nvvidconv`: NVMM or system memory, no advertised DMA-BUF output
- `glupload`: DMA-BUF, GLMemory, or system memory, no NVMM input
- current WebKit path: `webkitappsinkwithworkarounds`, selected because the
  installed GStreamer is older than fixes WebKit expects in 1.24

Result: `nvv4l2decoder` selection proves NVDEC availability but not browser
hardware decode. This stack needs an explicit NVMM-to-DMA-BUF/GLMemory bridge,
or at minimum an NVMM-to-system-memory conversion if performance is sufficient.

## Next go/no-go experiment

Build WebKitGTK/WPE with WebRTC enabled against GStreamer 1.24 or newer, then
test one stream in this order:

1. Confirm `RTCPeerConnection` and H.264 capabilities in JavaScript.
2. Confirm `nvv4l2decoder` without a software decoder in the pipeline.
3. Inspect decoder output memory, planes, stride, offsets, DRM modifier, and
   synchronization.
4. If NVMM is rejected, insert a small bridge that exports the underlying
   NvBufSurface as standard `GstDmaBufMemory` with complete video metadata.
5. Require the browser compositor to import the resulting DMA-BUF/GLMemory.
6. Only then expand from one stream to three streams plus Babylon.

The bridge must not merely rename the caps. It must preserve multi-plane NV12
layout, pitch or block-linear modifier, fences, buffer lifetime, and decoder
surface recycling.

