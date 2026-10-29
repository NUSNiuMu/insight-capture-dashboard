"""CameraRoutes HTTP handlers."""

from __future__ import annotations

import math
import shutil
import subprocess
import time
from typing import Dict, List

from aiohttp import web

from insight_capture.api.context import DashboardContext


class CameraRoutes:
    def __init__(self, context: DashboardContext) -> None:
        self.context = context

    async def _handle_camera_snapshot(self, _request: web.Request) -> web.Response:
        self.context.node.note_viewer_activity()
        payload = self.context.node.build_camera_payload()
        payload["playback_mode"] = (
            self.context.prepared_playback_manager.status()["state"] == "playing"
        )
        return web.json_response(payload)

    async def _handle_camera_frame(self, request: web.Request) -> web.Response:
        self.context.node.note_viewer_activity()
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
        self.context.node.note_viewer_activity()
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
            "sceneFps",
            "sceneMaxGapMs",
            "sceneWorkMaxMs",
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
        return web.json_response(self._build_image_capabilities())

    def _build_image_capabilities(self) -> Dict[str, object]:
        if self.context._image_capabilities_cache is None:
            self.context._image_capabilities_cache = self._detect_gstreamer_elements(
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
        elements = self.context._image_capabilities_cache
        has_webrtc = self.context.node._webrtc_available_cached
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
        # Live H.264 readiness comes from the worker; encoding paths describe JPEG output.
        hw_jpeg = getattr(self.context.node, "_hw_jpeg", None)
        encoders = self.context.node._image_pipeline.encoding_status()
        paths = {item["last_output_path"] for item in encoders.values() if item["recent"]}
        active_path = next(iter(paths)) if len(paths) == 1 else "mixed" if paths else "idle"
        notes = ["Per-camera encoding reports the last successful JPEG output; hardware availability is separate."]
        if hardware_encoder:
            notes.append(f"Hardware video encoder available to the WebRTC worker: {hardware_encoder}.")
        else:
            notes.append("No Jetson hardware H.264/H.265 encoder detected on this device.")
        if has_webrtc:
            notes.append("The WebRTC worker is ready.")
        else:
            notes.append("The WebRTC worker is currently not ready.")
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
            "hw_jpeg": {"available": hw_jpeg is not None, **(hw_jpeg.status() if hw_jpeg is not None else {})},
            "active_path": active_path,
            "cameras": [
                {
                    "name": camera.name,
                    "label": camera.label,
                    "topic": camera.topic,
                    "type": camera.topic_type,
                    "jpeg_encoding": encoders.get(camera.name),
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
