import json
import sys
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from insight_capture.web.routes.recording import RecordingRoutes  # noqa: E402


class _RecordingManager:
    def __init__(self, *, recording=False, output_path=None):
        self.recording = recording
        self.output_path = output_path
        self.start_calls = []
        self.stop_calls = 0

    def status(self):
        return {
            "recording": self.recording,
            "output_path": self.output_path,
            "merge_state": "idle",
        }

    def start(self, *, topics, bag_name):
        self.start_calls.append((topics, bag_name))
        self.recording = True
        self.output_path = f"/bags/{bag_name}"
        return self.status()

    def stop(self):
        self.stop_calls += 1
        self.recording = False
        return self.status()


class _Context:
    def __init__(self, manager):
        self.recording_manager = manager


def _payload(response):
    return json.loads(response.text)


class OpenClawAutomationRoutesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def direct(function, *args, **kwargs):
            return function(*args, **kwargs)

        patcher = mock.patch(
            "insight_capture.web.routes.recording.asyncio.to_thread",
            side_effect=direct,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_start_uses_default_topics_and_owned_prefix(self):
        manager = _RecordingManager()
        routes = RecordingRoutes(_Context(manager))

        response = await routes._handle_automation_recording_start(None)

        self.assertEqual(response.status, 200)
        self.assertEqual(manager.start_calls[0][0], None)
        self.assertTrue(manager.start_calls[0][1].startswith("looper_record_"))
        self.assertEqual(_payload(response)["automation"], "openclaw")

    async def test_start_refuses_to_replace_active_recording(self):
        manager = _RecordingManager(recording=True, output_path="/bags/manual")
        routes = RecordingRoutes(_Context(manager))

        response = await routes._handle_automation_recording_start(None)

        self.assertEqual(response.status, 409)
        self.assertEqual(manager.start_calls, [])

    async def test_stop_refuses_manual_recording(self):
        manager = _RecordingManager(
            recording=True,
            output_path="/bags/insight_record_20260814_100000",
        )
        routes = RecordingRoutes(_Context(manager))

        response = await routes._handle_automation_recording_stop(None)

        self.assertEqual(response.status, 409)
        self.assertEqual(manager.stop_calls, 0)

    async def test_stop_accepts_openclaw_owned_recording(self):
        manager = _RecordingManager(
            recording=True,
            output_path="/bags/looper_record_20260814_100000",
        )
        routes = RecordingRoutes(_Context(manager))

        response = await routes._handle_automation_recording_stop(None)

        self.assertEqual(response.status, 200)
        self.assertEqual(manager.stop_calls, 1)
        self.assertFalse(_payload(response)["recording"])


if __name__ == "__main__":
    unittest.main()
