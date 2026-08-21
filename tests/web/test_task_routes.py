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


class _JsonRequest:
    can_read_body = True

    def __init__(self, payload, *, task_id="") -> None:
        self.payload = payload
        self.match_info = {"task_id": task_id}

    async def json(self):
        return self.payload


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

    async def test_created_and_edited_task_is_persisted_in_its_task_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions_root = root / "sessions"
            catalog = CaptureTaskCatalog(
                [_catalog().get("cup_stacking")],
                default_task_id="cup_stacking",
                managed_root=sessions_root,
            )
            store = SessionTakeStore(root, task_catalog=catalog)
            routes = TaskRoutes(
                SimpleNamespace(
                    take_store=store,
                    recording_manager=_RecordingManager(),
                )
            )

            created = await routes._handle_create(
                _JsonRequest(
                    {
                        "id": "fold_towel",
                        "name": "Fold towel",
                        "speech_name": "叠毛巾",
                        "instruction": "Fold the towel in half",
                        "capture_profile": "dual_arm_umi",
                        "voice_aliases": ["叠毛巾", "折毛巾"],
                    }
                )
            )
            updated = await routes._handle_update(
                _JsonRequest(
                    {"name": "Fold the towel", "instruction": "Fold once"},
                    task_id="fold_towel",
                )
            )

            self.assertEqual(created.status, 201)
            self.assertEqual(json.loads(updated.text)["task"]["name"], "Fold the towel")
            task_path = sessions_root / "fold_towel" / "task.json"
            self.assertTrue(task_path.is_file())
            persisted = json.loads(task_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["instruction"], "Fold once")
            self.assertEqual(persisted["voice_aliases"], ["叠毛巾", "折毛巾"])
            config_path = root / "capture_tasks.json"
            config_path.write_text(
                json.dumps(
                    {
                        "default_task_id": "cup_stacking",
                        "tasks": [_catalog().get("cup_stacking").as_config_dict()],
                    }
                ),
                encoding="utf-8",
            )
            reloaded = CaptureTaskCatalog.load(
                config_path, managed_root=sessions_root
            )
            self.assertEqual(reloaded.get("fold_towel").name, "Fold the towel")

    async def test_recording_is_blocked_without_an_active_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = SessionTakeStore(root, task_catalog=_catalog())
            store.end_task()
            manager = _RecordingManager(root / "bags")
            routes = RecordingRoutes(
                SimpleNamespace(
                    take_store=store,
                    recording_manager=manager,
                    capture_preflight=None,
                )
            )

            response = await routes._start_with_preflight(
                topics=["/camera"], bag_name=None, automation=False
            )

            self.assertEqual(response.status, 409)
            self.assertEqual(manager.start_calls, [])
            self.assertIn("No capture task", json.loads(response.text)["error"])


if __name__ == "__main__":
    unittest.main()
