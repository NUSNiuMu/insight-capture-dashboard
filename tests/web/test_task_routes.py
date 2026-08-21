import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from insight_capture.api.routes.tasks import TaskRoutes  # noqa: E402
from insight_capture.api.routes.recording import RecordingRoutes  # noqa: E402
from insight_capture.runtime.take import SessionTakeStore  # noqa: E402
from insight_capture.runtime.tasks import CaptureTask, CaptureTaskCatalog  # noqa: E402


class _RecordingManager:
    def __init__(self, root: Path | None = None) -> None:
        self.recording = False
        self.root = root
        self.start_calls = []

    def status(self):
        return {"recording": self.recording}

    def start(self, **kwargs):
        self.start_calls.append(kwargs)
        output_path = (self.root or Path("/bags")) / str(kwargs.get("output_subdirectory") or "") / kwargs["bag_name"]
        self.recording = True
        return {"recording": True, "output_path": str(output_path)}


def _catalog() -> CaptureTaskCatalog:
    return CaptureTaskCatalog(
        [
            CaptureTask(
                task_id="cup_stacking",
                name="Cup stacking",
                speech_name="叠杯子",
                instruction="Stack the cups",
                capture_profile="dual_arm_umi",
                voice_aliases=("叠杯子",),
                station_check_after_take=True,
            )
        ],
        default_task_id="cup_stacking",
    )


class TaskRoutesTest(unittest.IsolatedAsyncioTestCase):
    async def test_status_reports_task_counts_and_next_take(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SessionTakeStore(Path(temporary), task_catalog=_catalog())
            take = store.reserve_take()
            store.mark_recording(Path(temporary) / take["bag_name"])
            store.complete_current({"output_path": take["bag_name"]})
            routes = TaskRoutes(
                SimpleNamespace(
                    take_store=store,
                    recording_manager=_RecordingManager(),
                )
            )

            response = await routes._handle_current(None)
            payload = json.loads(response.text)

            self.assertEqual(payload["task"]["name"], "Cup stacking")
            self.assertEqual(payload["stats"]["recorded_takes"], 1)
            self.assertEqual(payload["stats"]["next_take_id"], 2)
            self.assertIn("已录制1条", payload["speech"])

    async def test_task_change_is_rejected_while_recording(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = _RecordingManager()
            manager.recording = True
            routes = TaskRoutes(
                SimpleNamespace(
                    take_store=SessionTakeStore(
                        Path(temporary), task_catalog=_catalog()
                    ),
                    recording_manager=manager,
                )
            )
            request = SimpleNamespace(match_info={"task_id": "cup_stacking"})

            response = await routes._handle_activate(request)

            self.assertEqual(response.status, 409)
            self.assertIn("不能切换任务", json.loads(response.text)["speech"])

    async def test_recording_is_routed_into_active_task_set_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = SessionTakeStore(root, task_catalog=_catalog())
            manager = _RecordingManager(root / "bags")
            routes = RecordingRoutes(
                SimpleNamespace(
                    take_store=store,
                    recording_manager=manager,
                    capture_preflight=None,
                )
            )

            async def direct(function, *args, **kwargs):
                return function(*args, **kwargs)

            with mock.patch(
                "insight_capture.api.routes.recording.asyncio.to_thread",
                side_effect=direct,
            ):
                response = await routes._start_with_preflight(
                    topics=["/camera"], bag_name=None, automation=False
                )
            payload = json.loads(response.text)

            self.assertEqual(response.status, 200)
            self.assertEqual(
                manager.start_calls[0]["output_subdirectory"], "cup_stacking"
            )
            self.assertEqual(
                Path(payload["output_path"]).parent.name, "cup_stacking"
            )


if __name__ == "__main__":
    unittest.main()
