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
from insight_capture.api.routes.mobile import MobileCaptureRoutes  # noqa: E402
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


def _catalog(managed_root=None) -> CaptureTaskCatalog:
    return CaptureTaskCatalog(
        [
            CaptureTask(
                task_id="cup_stacking",
                name="Cup stacking",
                instruction="Stack the cups",
                capture_profile="dual_arm_umi",
            )
        ],
        default_task_id="cup_stacking",
        managed_root=managed_root,
    )


class TaskRoutesTest(unittest.IsolatedAsyncioTestCase):
    async def test_mobile_start_names_new_take_from_the_task_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = SessionTakeStore(root, task_catalog=_catalog(root / "tasks"))
            for task_id in ("task_original", "task_same_name"):
                store.create_task({
                    "id": task_id, "name": "堆叠/杯子", "instruction": "Stack cups",
                })
            store.activate_task("task_original")
            historical = store.reserve_take("task_original_take_0001_20260101_120000")
            old_bag = root / "bags" / "task_original" / historical["bag_name"]
            old_bag.mkdir(parents=True)
            store.mark_recording(old_bag)
            store.complete_current({"output_path": str(old_bag)})
            metadata = store.takes_root / "take_0001.json"
            old_take = json.loads(metadata.read_text())
            old_take.pop("recording_subdirectory", None)
            metadata.write_text(json.dumps(old_take))

            manager = _RecordingManager(root / "bags")
            context = SimpleNamespace(
                results_root=root,
                take_store=store,
                recording_manager=manager,
                capture_preflight=None,
                node=SimpleNamespace(get_logger=lambda: mock.Mock()),
            )
            mobile = MobileCaptureRoutes(context, RecordingRoutes(context))
            operation = {"command_id": "named-take", "task_id": "task_original"}
            await mobile._run("start", operation, None)

            self.assertEqual(operation.get("state"), "complete", operation)
            output = Path(operation["output_path"])
            self.assertTrue(output.parent.name.startswith("堆叠_杯子~"))
            self.assertTrue(output.name.startswith("堆叠_杯子_take_0002_"))
            self.assertEqual(output.parent.parent, root / "bags")
            tasks = {task["task_id"]: task for task in store.list_tasks()}
            self.assertNotEqual(
                tasks["task_original"]["recording_subdirectory"],
                tasks["task_same_name"]["recording_subdirectory"],
            )
            rows = store.task_recordings("task_original")["takes"]
            self.assertEqual({row["bag_path"] for row in rows}, {str(old_bag), str(output)})
            self.assertTrue(old_bag.is_dir())

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
            self.assertIn("已录制1条", payload["message"])

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
            self.assertIn("不能修改任务", json.loads(response.text)["message"])

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
                    topics=["/camera"], bag_name=None
                )
            payload = json.loads(response.text)

            self.assertEqual(response.status, 200)
            folder = store.recording_subdirectory()
            self.assertTrue(folder.startswith("Cup_stacking~"))
            self.assertEqual(
                manager.start_calls[0]["output_subdirectory"], folder
            )
            self.assertEqual(
                Path(payload["output_path"]).parent.name, folder
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
                        "instruction": "Fold the towel in half",
                        "capture_profile": "dual_arm_umi",
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
                topics=["/camera"], bag_name=None
            )

            self.assertEqual(response.status, 409)
            self.assertEqual(manager.start_calls, [])
            self.assertIn("No capture task", json.loads(response.text)["error"])


    async def test_stale_end_cannot_end_another_clients_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SessionTakeStore(Path(temporary), task_catalog=_catalog(Path(temporary) / "sessions"))
            store.create_task({"id": "another", "name": "Other", "instruction": "Other", "capture_profile": "dual_arm_umi"})
            store.activate_task("another")
            routes = TaskRoutes(SimpleNamespace(take_store=store, recording_manager=_RecordingManager()))
            before = (store.sessions_root / "active_session.json").read_bytes()
            response = await routes._handle_end(_JsonRequest({"task_id": "cup_stacking"}))
            self.assertEqual(response.status, 409)
            self.assertEqual(store.task_status()["task"]["task_id"], "another")
            self.assertEqual((store.sessions_root / "active_session.json").read_bytes(), before)

    async def test_end_requires_the_task_displayed_by_the_client(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SessionTakeStore(Path(temporary), task_catalog=_catalog())
            routes = TaskRoutes(SimpleNamespace(take_store=store, recording_manager=_RecordingManager()))
            for task_id in (None, "", "  ", 42, []):
                response = await routes._handle_end(_JsonRequest({"task_id": task_id}))
                self.assertEqual(response.status, 400)
                self.assertTrue(store.task_status()["active"])

    async def test_end_same_task_is_idempotent_without_deleting_takes(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SessionTakeStore(Path(temporary), task_catalog=_catalog())
            routes = TaskRoutes(SimpleNamespace(take_store=store, recording_manager=_RecordingManager()))
            for _ in range(2):
                response = await routes._handle_end(_JsonRequest({"task_id": "cup_stacking"}))
                self.assertEqual(response.status, 200)
                self.assertFalse(json.loads(response.text)["active"])
                self.assertEqual(json.loads(response.text)["last_task_id"], "cup_stacking")
            self.assertEqual(store.task_status()["stats"]["next_take_id"], 1)
            self.assertTrue(store.root.is_dir())

    async def test_task_change_during_end_snapshot_is_rechecked_under_store_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SessionTakeStore(Path(temporary), task_catalog=_catalog(Path(temporary) / "sessions"))
            store.create_task({"id": "another", "name": "Other", "instruction": "Other", "capture_profile": "dual_arm_umi"})
            routes = TaskRoutes(SimpleNamespace(take_store=store, recording_manager=_RecordingManager()))
            original_end = store.end_task
            def concurrent_change(**kwargs):
                store.activate_task("another")
                return original_end(**kwargs)
            with mock.patch.object(store, "end_task", side_effect=concurrent_change):
                with self.assertRaisesRegex(RuntimeError, "当前任务已变化"):
                    await routes._handle_end(_JsonRequest({"task_id": "cup_stacking"}))
            self.assertTrue(store.task_status()["active"])
            self.assertEqual(store.task_status()["task"]["task_id"], "another")

    async def test_end_remains_blocked_during_recording(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SessionTakeStore(Path(temporary), task_catalog=_catalog())
            manager = _RecordingManager(); manager.recording = True
            routes = TaskRoutes(SimpleNamespace(take_store=store, recording_manager=manager))
            response = await routes._handle_end(_JsonRequest({"task_id": "cup_stacking"}))
            self.assertEqual(response.status, 409)
            self.assertTrue(store.task_status()["active"])


if __name__ == "__main__":
    unittest.main()
