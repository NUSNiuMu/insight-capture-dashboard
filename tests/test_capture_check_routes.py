import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from dashboard_web.routes.capture_check import CaptureCheckRoutes  # noqa: E402


class _RecordingManager:
    def __init__(self, recording=False):
        self.recording = recording

    def status(self):
        return {
            "recording": self.recording,
            "output_path": "/bags/cup_stack_001",
        }


class _Node:
    def __init__(self):
        self.calls = []

    def capture_check_status(self, *, bag_name):
        self.calls.append(("status", bag_name))
        return {"state": "ready", "bag_name": bag_name}

    def set_capture_check_reference(self):
        self.calls.append(("reference", None))
        return {"state": "reference_saved"}

    def run_capture_check(self, *, bag_name):
        self.calls.append(("check", bag_name))
        return {"state": "pass", "bag_name": bag_name}


class _Context:
    def __init__(self, recording=False):
        self.recording_manager = _RecordingManager(recording)
        self.node = _Node()


def _payload(response):
    return json.loads(response.text)


class CaptureCheckRoutesTest(unittest.IsolatedAsyncioTestCase):
    async def test_check_is_associated_with_latest_bag(self):
        context = _Context()
        response = await CaptureCheckRoutes(context)._handle_run(None)
        self.assertEqual(_payload(response)["state"], "pass")
        self.assertEqual(context.node.calls, [("check", "cup_stack_001")])

    async def test_reference_can_be_saved_while_idle(self):
        context = _Context()
        response = await CaptureCheckRoutes(context)._handle_reference(None)
        self.assertEqual(_payload(response)["state"], "reference_saved")

    async def test_active_recording_blocks_station_check(self):
        context = _Context(recording=True)
        response = await CaptureCheckRoutes(context)._handle_run(None)
        self.assertEqual(_payload(response)["state"], "not_ready")
        self.assertEqual(context.node.calls, [])


if __name__ == "__main__":
    unittest.main()
