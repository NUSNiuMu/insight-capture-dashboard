import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from insight_capture.api.routes.cameras import CameraRoutes
from insight_capture.api.routes.native import NativePreviewRoutes
from insight_capture.core.config import build_dashboard_config, load_setup
from insight_capture.media.native_preview import FORMATS, HEADER, NativePreviewHub
from insight_capture.runtime.payloads import PayloadBuilder


# A JPEG start-of-frame header with the Insight9 portrait dimensions.
JPEG = bytes.fromhex("ffd8 ffc0 000b 08 0780 0440 01 01 11 00 ffd9")


def test_default_jetson_head_streams_jpeg_to_local_qt_without_webrtc():
    async def check():
        root = Path(__file__).resolve().parents[2]
        profile = load_setup(root / "config/devices/jetson-nx/cameras.json")
        cameras = [
            SimpleNamespace(**{"width": 0, "height": 0, **c, "topic_type": c["type"]})
            for c in build_dashboard_config(profile)["cameras"]
        ]
        head = next(c for c in cameras if c.name == "insight9_a")
        assert (head.width, head.height) == (0, 0)
        hub = NativePreviewHub(cameras)
        node = SimpleNamespace(
            cameras=cameras, native_preview=hub, camera_stale_timeout_sec=2,
            camera_input_lock=threading.Lock(), camera_frame_lock=threading.Lock(),
            camera_input_times={}, camera_liveness_times={}, latest_camera_frames={},
            camera_frame_times={}, _webrtc_metrics_lock=threading.Lock(),
            _webrtc_main_metrics={}, _webrtc_worker_stats={}, _webrtc_browser_stats={},
            _webrtc_available_cached=False, webrtc_port=8766,
            _preview_manager=SimpleNamespace(paused=False),
            preview_status=lambda: {"webrtc_worker_running": False},
            viewer_connected=lambda: None, viewer_disconnected=lambda: None,
        )
        node.build_camera_payload = PayloadBuilder(node).build_camera_payload
        context = SimpleNamespace(node=node, prepared_playback_manager=SimpleNamespace(
            status=lambda: {"state": "idle"},
        ))
        app = web.Application()
        app.router.add_get("/api/cameras", CameraRoutes(context)._handle_camera_snapshot)
        app.router.add_get("/ws/native", NativePreviewRoutes(context)._handle_stream)
        async with TestClient(TestServer(app)) as client:
            async with client.get("/api/cameras") as response:
                payload = await response.json()
            by_name = {camera["name"]: camera for camera in payload["cameras"]}
            assert by_name[head.name]["native_preview_available"]
            assert not by_name[head.name]["webrtc_available"]
            assert by_name["insight3_a"]["native_preview_available"]
            assert by_name["insight3_b"]["native_preview_available"]

            async with client.ws_connect(f"/ws/native?camera={head.name}&fps=30") as ws:
                stamp = SimpleNamespace(sec=1, nanosec=2)
                frame = SimpleNamespace(header=SimpleNamespace(stamp=stamp), data=JPEG[:9])
                hub.submit(head.name, frame)
                # Let the sender consume the malformed frame before offering a valid one.
                await asyncio.sleep(0.05)
                hub.submit(head.name, SimpleNamespace(header=frame.header, data=JPEG))
                message = await ws.receive(timeout=2)
                assert message.type == WSMsgType.BINARY
                assert HEADER.unpack_from(message.data)[:7] == (
                    b"INPV", 1, FORMATS["JPEG"], 0, 1088, 1920, 1_000_000_002,
                )
                assert message.data[HEADER.size:] == JPEG

    asyncio.run(check())
