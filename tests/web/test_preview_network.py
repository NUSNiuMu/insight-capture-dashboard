import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from insight_capture.api.routes.preview import signal_on_address
from insight_capture.media import webrtc
from insight_capture.media.webrtc import WebRtcStreams


class PreviewNetworkTest(unittest.IsolatedAsyncioTestCase):
    def test_nvmm_initialization_falls_back_for_the_rest_of_the_worker(self):
        source_ids = ("left", "stereo_left", "stereo_right", "right")
        overview = {"width": 960, "height": 1280, "regions": [{"id": name} for name in source_ids]}
        streams = WebRtcStreams({"sources": [], "overview": overview}, 8766, lambda *_: None,
                               log=lambda *_: None, passthrough=source_ids)
        native_attempts = []
        cpu_encoder = object()

        def unavailable(*_):
            native_attempts.append("nvmm")
            raise RuntimeError("hardware compositor unavailable")

        with patch.dict(sys.modules, {
            "insight_capture.media.nvmm_preview": SimpleNamespace(NvmmOverviewEncoder=unavailable),
            "insight_capture.media.jetson_preview": SimpleNamespace(OverviewEncoder=lambda *_, software_jpeg_sources: cpu_encoder),
        }):
            self.assertIs(streams._new_encoder("__overview__", lambda *_: None), cpu_encoder)
            self.assertIs(streams._new_encoder("__overview__", lambda *_: None), cpu_encoder)
        self.assertEqual(native_attempts, ["nvmm"])

    async def test_nvmm_runtime_failure_falls_back_then_retries_for_a_new_encoder(self):
        source_ids = ("left", "stereo_left", "stereo_right", "right")
        overview = {"width": 960, "height": 1280, "regions": [{"id": name} for name in source_ids]}
        streams = WebRtcStreams({"sources": [], "overview": overview}, 8766, lambda *_: None,
                               log=lambda *_: None, passthrough=source_ids)
        streams.loop = asyncio.get_running_loop()
        streams.watchers["__overview__"] = 1
        app = web.Application()
        app.router.add_get("/stream/{source}.h264", streams.h264)
        native = SimpleNamespace(native_overview=True, error="decoder failed", close=lambda: None)
        recovered = SimpleNamespace(native_overview=True, error=None, close=lambda: None)
        native_encoders = iter((native, recovered))
        clock = 100.0
        factories = {
            "insight_capture.media.nvmm_preview": SimpleNamespace(NvmmOverviewEncoder=lambda *_: next(native_encoders)),
            "insight_capture.media.jetson_preview": SimpleNamespace(
                OverviewEncoder=lambda _desc, deliver, _log, *, software_jpeg_sources: SimpleNamespace(deliver=deliver, close=lambda: None)),
        }
        with patch.dict(sys.modules, factories), patch.object(webrtc, "monotonic", lambda: clock, create=True):
            async with TestClient(TestServer(app)) as client:
                failed = await client.get("/stream/__overview__.h264")
                self.assertEqual(await asyncio.wait_for(failed.read(), 2), b"")
                regular = await client.get("/stream/__overview__.h264")
                streams.producers["__overview__"].deliver(b"keyframe", True)
                self.assertEqual(await asyncio.wait_for(regular.content.readexactly(8), 2), b"keyframe")
                regular.close()
                clock += 30
                self.assertIs(streams._new_encoder("__overview__", lambda *_: None), recovered)

    async def test_closing_last_overview_viewer_does_not_trigger_hardware_fallback(self):
        source_ids = ("left", "stereo_left", "stereo_right", "right")
        overview = {"regions": [{"id": name} for name in source_ids]}
        events = []
        streams = WebRtcStreams({"sources": [], "overview": overview}, 8766, lambda *_: None,
                               log=events.append, passthrough=source_ids)
        streams.loop = asyncio.get_running_loop()
        streams.watchers["__overview__"] = 1
        app = web.Application()
        app.router.add_get("/stream/{source}.h264", streams.h264)

        def native_encoder(_description, deliver, _log):
            return SimpleNamespace(native_overview=True, error=None, deliver=deliver, close=lambda: None)

        factories = {
            "insight_capture.media.nvmm_preview": SimpleNamespace(NvmmOverviewEncoder=native_encoder),
            "insight_capture.media.jetson_preview": SimpleNamespace(OverviewEncoder=lambda *_, software_jpeg_sources: object()),
        }
        with patch.dict(sys.modules, factories):
            async with TestClient(TestServer(app)) as client:
                response = await client.get("/stream/__overview__.h264")
                streams.producers["__overview__"].deliver(b"keyframe", True)
                self.assertEqual(await asyncio.wait_for(response.content.readexactly(8), 2), b"keyframe")
                streams.watchers["__overview__"] = 0
                self.assertEqual(await asyncio.wait_for(response.read(), 4), b"")
                self.assertEqual(events, [])
                self.assertTrue(streams._new_encoder("__overview__", lambda *_: None).native_overview)

    async def test_h264_focus_forwards_the_source_bytes_starting_at_idr(self):
        description = {"sources": [{"id": "head", "width": 1200, "height": 1600}], "overview": None}
        streams = WebRtcStreams(description, 8766, lambda *_: None, passthrough=["head"])
        streams.loop = asyncio.get_running_loop()
        streams.watchers["head"] = 1
        app = web.Application()
        app.router.add_get("/stream/{source}.h264", streams.h264)
        inter = b"\x00\x00\x00\x01\x41inter"
        idr = b"\x00\x00\x00\x01\x67sps\x00\x00\x00\x01\x68pps\x00\x00\x00\x01\x65idr"
        with patch.dict(sys.modules, {"insight_capture.media.jetson_preview": None}):
            async with TestClient(TestServer(app)) as client:
                response = await client.get("/stream/head.h264")
                streams.push_resolved_frame("head", "H264", 1200, 1600, inter, 1)
                streams.push_resolved_frame("head", "H264", 1200, 1600, idr, 2)
                streams.push_resolved_frame("head", "H264", 1200, 1600, inter, 3)
                received = await asyncio.wait_for(response.content.readexactly(len(idr) + len(inter)), 2)
                self.assertEqual(received, idr + inter)
                self.assertFalse(streams.producers)
                response.close()

    def test_signalling_uses_connection_addresses(self):
        def candidate(address):
            return f"candidate:1 1 UDP 2130706431 {address} 8768 typ host"

        for address in ("192.168.55.1", "10.77.0.1", "192.168.60.94"):
            candidates = [candidate(ip) for ip in (address, "192.168.19.222", "hidden.local")]
            sdp = "v=0\r\na=rtpmap:96 H264/90000\r\n" + "".join(f"a={c}\r\n" for c in candidates)
            for kind in ("webrtc/offer", "webrtc/answer"):
                filtered = json.loads(signal_on_address(json.dumps({"type": kind, "value": sdp}), address))["value"]
                self.assertIn(candidates[0], filtered)
                self.assertNotIn(candidates[1], filtered)
                self.assertNotIn(candidates[2], filtered)
                self.assertIn("H264/90000\r\n", filtered)
            for index, value in enumerate(candidates):
                result = signal_on_address(json.dumps({"type": "webrtc/candidate", "value": value}), address)
                self.assertEqual(result is not None, index == 0)

    async def test_two_network_relays_share_encoder_until_last_consumer(self):
        encoders = []

        class Encoder:
            def __init__(self, description, deliver, log, *, software_jpeg_sources):
                self.deliver = deliver
                self.closed = 0
                encoders.append(self)

            def close(self):
                self.closed += 1

        streams = WebRtcStreams({"sources": [{"id": "camera", "width": 2, "height": 2}], "overview": None}, 8766, lambda *args: None)
        streams.loop = asyncio.get_running_loop()
        streams.watchers["camera"] = 2
        app = web.Application()
        app.router.add_get("/stream/{source}.h264", streams.h264)
        fake_encoder_module = SimpleNamespace(OverviewEncoder=Encoder)
        with patch.dict(sys.modules, {"insight_capture.media.jetson_preview": fake_encoder_module}):
            async with TestClient(TestServer(app)) as client:
                first = await client.get("/stream/camera.h264")
                second = await client.get("/stream/camera.h264")
                self.assertEqual(len(encoders), 1)
                encoders[0].deliver(b"undecodable-inter-frame", False)
                encoders[0].deliver(b"frame-one", True)
                self.assertEqual(await asyncio.wait_for(first.content.readexactly(9), 2), b"frame-one")
                self.assertEqual(await asyncio.wait_for(second.content.readexactly(9), 2), b"frame-one")
                first.close()
                for _ in range(20):
                    encoders[0].deliver(b"next", False)
                    await asyncio.wait_for(second.content.readexactly(4), 2)
                    if len(streams.listeners["camera"]) == 1:
                        break
                    await asyncio.sleep(.01)
                self.assertEqual(len(streams.listeners["camera"]), 1)
                self.assertEqual(encoders[0].closed, 0)
                streams.watchers["camera"] = 0
                encoders[0].deliver(b"last", False)
                await asyncio.wait_for(second.read(), 2)
                self.assertEqual(encoders[0].closed, 1)
                self.assertFalse(streams.producers)


if __name__ == "__main__":
    unittest.main()
