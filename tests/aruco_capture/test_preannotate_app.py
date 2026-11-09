import asyncio
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from insight_capture.aruco_capture.app import create_app


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"status": "ok", "model": "fake-vl"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def fake_capture(root, name):
    session = Path(root) / name
    session.mkdir()
    (session / "session.json").write_text(json.dumps({"status": "stopped", "duration_s": 1}))
    return SimpleNamespace(root=Path(root), active=None, stop=lambda: name, close=lambda: None)


async def stop_and_read_draft(capture, url):
    async with TestClient(TestServer(create_app(capture, url))) as client:
        stopped = await (await client.post("/api/stop")).json()
        # Probing and the job both run in the background: wait until a job has
        # finished, or long enough that a skipped one would have shown up.
        for _ in range(20):
            await asyncio.sleep(0.1)
            state = await (await client.get(f"/api/sessions/{stopped['stopped']}/draft")).json()
            if state["job"] and not state["job"]["running"]:
                break
        return stopped, state


class StopTriggersPreannotationTests(unittest.TestCase):
    def test_skipped_when_service_down(self):
        with tempfile.TemporaryDirectory() as root:
            stopped, state = asyncio.run(
                stop_and_read_draft(fake_capture(root, "s1"), "http://127.0.0.1:9")
            )
        self.assertEqual(stopped, {"stopped": "s1"})
        self.assertEqual(state, {"draft": None, "job": None})

    def test_skipped_when_not_configured(self):
        with tempfile.TemporaryDirectory() as root:
            _, state = asyncio.run(stop_and_read_draft(fake_capture(root, "s1"), ""))
        self.assertIsNone(state["job"])

    def test_queued_when_service_up(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Health)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as root:
                _, state = asyncio.run(
                    stop_and_read_draft(
                        fake_capture(root, "s1"), f"http://127.0.0.1:{server.server_port}"
                    )
                )
        finally:
            server.shutdown()
        job = state["job"]
        self.assertEqual(job["model"], "fake-vl")
        self.assertFalse(job["running"])
        # The fake session has no recorded data, so the run itself fails —
        # and that is reported on the job rather than raised.
        self.assertTrue(job["error"])
        self.assertIsNone(state["draft"])


class ServiceSettingTests(unittest.TestCase):
    def test_set_from_page(self):
        async def go(capture):
            async with TestClient(TestServer(create_app(capture, "http://127.0.0.1:9"))) as client:
                first = await (await client.get("/api/annotation/service")).json()
                changed = await (
                    await client.post("/api/annotation/service", json={"url": "127.0.0.1:1"})
                ).json()
                bad = await client.post("/api/annotation/service", json={"url": "ftp://x"})
                return first, changed, bad.status

        with tempfile.TemporaryDirectory() as root:
            first, changed, bad = asyncio.run(go(fake_capture(root, "s1")))
            saved = json.loads((Path(root) / "_annotation.json").read_text())
        self.assertEqual(first["url"], "http://127.0.0.1:9")
        self.assertEqual(changed, {"configured": True, "url": "http://127.0.0.1:1", "health": None})
        self.assertEqual(saved, {"url": "http://127.0.0.1:1"})
        self.assertEqual(bad, 400)


if __name__ == "__main__":
    unittest.main()
