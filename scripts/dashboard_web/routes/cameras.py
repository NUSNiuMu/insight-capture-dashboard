"""CameraRoutes HTTP handlers."""

from __future__ import annotations

import math
import shutil
import subprocess
import time
from typing import Dict, List

from aiohttp import web

from dashboard_web.context import DashboardContext


class CameraRoutes:
    def __init__(self, context: DashboardContext) -> None:
        self.context = context

    async def _handle_camera_snapshot(self, _request: web.Request) -> web.Response:
        payload = self.context.node.build_camera_payload()
        payload["playback_mode"] = self.context.playback_manager.status()["state"] == "playing"
        return web.json_response(payload)

    async def _handle_camera_frame(self, request: web.Request) -> web.Response:
        camera_name = request.match_info.get("camera_name", "")
        frame = self.context.node.latest_camera_frame(camera_name)
        if frame is None:
            raise web.HTTPNotFound(text="camera frame not available yet")
        headers = {
            "Cache-Control": "no-store, max-age=0",
            "X-Frame-Stamp-Ns": str(frame.stamp_ns),
            "X-Frame-Version": str(frame.version),
        }
        return web.Response(body=frame.data, content_type=frame.mime_type, headers=headers)

    async def _handle_browser_stats(self, request: web.Request) -> web.Response:
        camera_name = request.match_info.get("camera_name", "")
        if camera_name not in {camera.name for camera in self.context.node.cameras}:
            raise web.HTTPNotFound(text="unknown camera")
        payload = await request.json()
        allowed = (
            "framesReceived",
            "framesDecoded",
            "framesDropped",
            "packetsReceived",
            "packetsLost",
            "bytesReceived",
            "receivedFps",
            "decodedFps",
            "presentedFps",
            "jitterMs",
        )
        stats = {}
        for key in allowed:
            try:
                value = float(payload.get(key, 0))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                stats[key] = value
        stats["updated_monotonic"] = time.monotonic()
        with self.context.node._webrtc_metrics_lock:
            self.context.node._webrtc_browser_stats[camera_name] = stats
        return web.Response(status=204)

    async def _handle_image_capabilities(self, _request: web.Request) -> web.Response:
        # GStreamer capabilities are stable for the process lifetime.
        if self.context._image_capabilities_cache is None:
            self.context._image_capabilities_cache = self._build_image_capabilities()
        return web.json_response(self.context._image_capabilities_cache)

    def _build_image_capabilities(self) -> Dict[str, object]:
        elements = self._detect_gstreamer_elements(
            [
                "webrtcbin",
                "nice",
                "nvv4l2h264enc",
                "nvv4l2h265enc",
                "nvv4l2decoder",
                "nvjpegenc",
                "nvjpegdec",
                "nvvidconv",
                "openh264enc",
                "x264enc",
                "vp8enc",
            ]
        )
        has_webrtc = bool(elements.get("webrtcbin") and elements.get("nice"))
        hardware_encoder = None
        if elements.get("nvv4l2h264enc"):
            hardware_encoder = "nvv4l2h264enc"
        elif elements.get("nvv4l2h265enc"):
            hardware_encoder = "nvv4l2h265enc"
        software_encoder = None
        for candidate in ("openh264enc", "x264enc", "vp8enc"):
            if elements.get(candidate):
                software_encoder = candidate
                break
        # Live H.264 status comes from the worker; this reports JPEG fallback.
        hw_jpeg = getattr(self.context.node, "_hw_jpeg", None)
        active_path = "jpeg-hardware-nvjpeg" if hw_jpeg is not None else "jpeg-software"
        notes = []
        if hw_jpeg is not None:
            notes.append("Display frames are encoded on the NVJPEG hardware engine (nvjpegenc).")
        else:
            notes.append("Display frames are encoded in software (cv2); NVJPEG path unavailable.")
        if hardware_encoder:
            notes.append(f"Hardware video encoder available to the WebRTC worker: {hardware_encoder}.")
        else:
            notes.append("No Jetson hardware H.264/H.265 encoder detected on this device.")
        if has_webrtc:
            notes.append("WebRTC transport dependencies are present; signaling runs in the worker process.")
        else:
            notes.append("WebRTC transport is incomplete; install gstreamer1.0-nice if nice is missing.")
        return {
            "type": "image_capabilities",
            "gstreamer": {
                "available": shutil.which("gst-inspect-1.0") is not None,
                "elements": elements,
            },
            "webrtc_ready": has_webrtc,
            "hardware_encoder": hardware_encoder,
            "software_encoder": software_encoder,
            "decode_acceleration": {
                "nvjpegdec": bool(elements.get("nvjpegdec")),
                "nvv4l2decoder": bool(elements.get("nvv4l2decoder")),
                "nvvidconv": bool(elements.get("nvvidconv")),
            },
            "hw_jpeg": {"active": hw_jpeg is not None, **(hw_jpeg.status() if hw_jpeg is not None else {})},
            "active_path": active_path,
            "cameras": [
                {
                    "name": camera.name,
                    "label": camera.label,
                    "topic": camera.topic,
                    "type": camera.topic_type,
                }
                for camera in self.context.node.cameras
            ],
            "notes": notes,
        }

    @staticmethod
    def _detect_gstreamer_elements(elements: List[str]) -> Dict[str, bool]:
        if shutil.which("gst-inspect-1.0") is None:
            return {element: False for element in elements}
        detected = {}
        for element in elements:
            try:
                result = subprocess.run(
                    ["gst-inspect-1.0", element],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2.0,
                )
                detected[element] = result.returncode == 0
            except Exception:
                detected[element] = False
        return detected
