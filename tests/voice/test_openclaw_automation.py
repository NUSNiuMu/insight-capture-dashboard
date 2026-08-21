import json
import sys
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from insight_capture.api.routes.recording import RecordingRoutes  # noqa: E402


class _RecordingManager:
    def __init__(self, *, recording=False, output_path=None):
        self.recording = recording
        self.output_path = output_path
        self.start_calls = []
        self.stop_calls = 0
        self.recording_mode = None
        self.payload_report = {"ok": True, "topics": {}, "missing": []}

    def status(self):
        return {
            "recording": self.recording,
            "output_path": self.output_path,
            "merge_state": "idle",
            "recording_mode": self.recording_mode,
        }

    def start(self, *, topics, bag_name, recording_mode="capture"):
        self.start_calls.append((topics, bag_name))
        self.recording = True
        self.recording_mode = recording_mode
        self.output_path = f"/bags/{bag_name}"
        return self.status()

    def probe_topic_payloads(self, topics):
        self.probed_topics = list(topics)
        return self.payload_report

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
            "insight_capture.api.routes.recording.asyncio.to_thread",
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

    async def test_vio_calibration_start_uses_fixed_17_topic_contract(self):
        manager = _RecordingManager()
        routes = RecordingRoutes(_Context(manager))

        response = await routes._handle_automation_vio_calibration_start(None)

        self.assertEqual(response.status, 200)
        topics, bag_name = manager.start_calls[0]
        self.assertEqual(len(topics), 17)
        self.assertEqual(topics[-1], "/tf_static")
        self.assertNotIn("/insight_global/insight3_a/pose", topics)
        self.assertTrue(bag_name.startswith("vio_calibration_"))
        self.assertEqual(manager.recording_mode, "vio_calibration")
        self.assertEqual(len(manager.probed_topics), 4)

    async def test_vio_calibration_start_rejects_source_silent_raw_images(self):
        manager = _RecordingManager()
        manager.payload_report = {
            "ok": False,
            "topics": {},
            "missing": ["/insight3_b/camera/infra2/image_raw"],
        }
        routes = RecordingRoutes(_Context(manager))

        response = await routes._handle_automation_vio_calibration_start(None)

        self.assertEqual(response.status, 422)
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

    async def test_stop_accepts_vio_calibration_recording(self):
        manager = _RecordingManager(
            recording=True,
            output_path="/bags/vio_calibration_20260821_100000",
        )
        manager.recording_mode = "vio_calibration"
        routes = RecordingRoutes(_Context(manager))

        response = await routes._handle_automation_recording_stop(None)

        self.assertEqual(response.status, 200)
        self.assertEqual(manager.stop_calls, 1)


if __name__ == "__main__":
    unittest.main()
