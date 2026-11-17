import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from insight_capture.api.context import capture_transition_lock
from insight_capture.api.routes.tasks import TaskRoutes
from insight_capture.runtime.take import SessionTakeStore
from insight_capture.runtime.tasks import CaptureTaskCatalog

class TaskDeletionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "tasks.json"
        self.config.write_text(json.dumps({"default_task_id":"builtin", "tasks":[
            {"id":"builtin","name":"Builtin","instruction":"Do it"}]}))
        self.sessions = self.root / "results" / "sessions"
        self.catalog = CaptureTaskCatalog.load(self.config, managed_root=self.sessions)
        self.store = SessionTakeStore(self.root / "results", task_catalog=self.catalog)
        self.raw = self.root / "bags"
        self.raw.mkdir()
        self.manager = SimpleNamespace(status=lambda:{"recording":False}, storage_browse_roots=lambda:[self.raw])
        self.context = SimpleNamespace(take_store=self.store,recording_manager=self.manager)
        self.routes = TaskRoutes(self.context)

    async def delete(self, task="builtin", count=0):
        class Request:
            can_read_body=True
            match_info={"task_id":task}
            async def json(self): return {"expected_recorded_takes":count}
        return await self.routes._handle_delete(Request())

    def record(self):
        take=self.store.reserve_take()
        bag=self.raw / "builtin" / take["bag_name"]
        bag.mkdir(parents=True)
        (bag / "data.mcap").write_bytes(b"synthetic test data")
        self.store.mark_recording(bag)
        self.store.complete_current({"output_path":str(bag)})
        return bag

    async def test_removes_recordings_and_builtin_persistently_preserves_other_task(self):
        bag=self.record()
        self.store.create_task({"id":"other","name":"Other","instruction":"Keep"})
        sentinel=self.raw / "other" / "keep";sentinel.parent.mkdir();sentinel.write_text("keep")
        response=await self.delete(count=1)
        self.assertEqual(response.status,200,response.text)
        self.assertFalse(bag.exists())
        self.assertFalse((self.sessions / "builtin").exists())
        self.assertEqual(sentinel.read_text(),"keep")
        catalog=CaptureTaskCatalog.load(self.config,managed_root=self.sessions)
        restored=SessionTakeStore(self.root / "results",task_catalog=catalog)
        self.assertFalse(restored.task_status()["active"])
        self.assertEqual([t["task_id"] for t in catalog.list()],["other"])
        self.assertEqual((await self.delete(count=1)).status,404)

    async def test_managed_task_stays_deleted_after_reload(self):
        self.store.create_task({"id":"qa","name":"QA","instruction":"Test"})
        self.assertEqual((await self.delete("qa")).status,200)
        self.assertEqual([t["task_id"] for t in CaptureTaskCatalog.load(self.config,managed_root=self.sessions).list()],["builtin"])
        self.assertTrue(self.store.task_status()["active"])

    async def test_stale_count_and_active_take_preserve_files(self):
        bag=self.record()
        self.assertEqual((await self.delete()).status,409)
        self.assertTrue(bag.exists())
        self.store.reserve_take()
        self.assertEqual((await self.delete(count=1)).status,409)
        self.assertTrue(bag.exists())

    async def test_recording_and_transition_reject_delete(self):
        self.manager.status=lambda:{"recording":True}
        self.assertEqual((await self.delete()).status,409)
        self.manager.status=lambda:{"recording":False}
        async with capture_transition_lock(self.context):
            self.assertEqual((await self.delete()).status,409)
        self.manager.status=lambda:{"recording":False,"merge_state":"merging"}
        self.assertEqual((await self.delete()).status,409)

    async def test_symlink_or_mount_preserves_all_targets(self):
        outside=self.root / "outside";outside.mkdir();(outside / "keep").write_text("keep")
        (self.raw / "builtin").symlink_to(outside,target_is_directory=True)
        self.assertEqual((await self.delete()).status,409)
        self.assertTrue((outside / "keep").exists())
        self.assertTrue((self.sessions / "builtin").exists())
        (self.raw / "builtin").unlink();(self.raw / "builtin").mkdir()
        with patch.object(Path,"is_mount",lambda p:p == self.raw / "builtin"):
            self.assertEqual((await self.delete()).status,409)
        self.assertTrue((self.sessions / "builtin").exists())

    async def test_rejects_boolean_or_missing_confirmation(self):
        for value in [True,-1,None,"0"]:
            self.assertEqual((await self.delete(count=value)).status,400)

    async def test_cancelled_request_keeps_transition_until_worker_finishes(self):
        entered=threading.Event();finish=threading.Event()
        original=self.store.delete_task
        def slow(*args):
            entered.set();finish.wait(3);return original(*args)
        with patch.object(self.store,"delete_task",slow):
            request=asyncio.create_task(self.delete())
            await asyncio.to_thread(entered.wait,2)
            request.cancel()
            await asyncio.sleep(.02)
            self.assertTrue(capture_transition_lock(self.context).locked())
            self.assertFalse(request.done())
            finish.set()
            with self.assertRaises(asyncio.CancelledError):await request
        self.assertFalse(capture_transition_lock(self.context).locked())

    async def test_playback_only_blocks_the_selected_task(self):
        bag=self.record()
        current={"bag_name":"bag","state":"playing"}
        self.context.prepared_playback_manager=SimpleNamespace(status=lambda:current,stop=lambda:current.update(state="idle"))
        self.context.bag_library=SimpleNamespace(resolve=lambda name:bag)
        self.assertEqual((await self.delete(count=1)).status,409)
        self.store.create_task({"id":"qa","name":"QA","instruction":"Test"})
        self.assertEqual((await self.delete("qa")).status,200)
        current["state"]="ready"
        self.assertEqual((await self.delete(count=1)).status,200)
        self.assertEqual(current["state"],"idle")
