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
from insight_capture.media.webrtc import WebRtcStreams


class PreviewNetworkTest(unittest.IsolatedAsyncioTestCase):
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
            def __init__(self, description, deliver, log):
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
