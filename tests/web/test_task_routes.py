import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from insight_capture.api.routes.tasks import TaskRoutes  # noqa: E402
from insight_capture.runtime.take import SessionTakeStore  # noqa: E402
from insight_capture.runtime.tasks import CaptureTask, CaptureTaskCatalog  # noqa: E402


class _RecordingManager:
    def __init__(self) -> None:
        self.recording = False

    def status(self):
        return {"recording": self.recording}


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


if __name__ == "__main__":
    unittest.main()
