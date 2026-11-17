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

    async def delete_take(self, take_id, bag_name, task="builtin"):
        class Request:
            can_read_body=True
            match_info={"task_id":task,"take_id":str(take_id)}
            async def json(self): return {"bag_name":bag_name}
        return await self.routes._handle_take_delete(Request())

    async def test_single_delete_keeps_task_and_other_take_bytes(self):
        first=self.record();second=self.record()
        second_meta=self.sessions / "builtin/takes/take_0002.json"
        saved=second_meta.read_bytes()
        response=await self.delete_take(1,first.name)
        self.assertEqual(response.status,200,response.text)
        self.assertFalse(first.exists())
        self.assertFalse((self.sessions / "builtin/takes/take_0001.json").exists())
        self.assertEqual(second_meta.read_bytes(),saved)
        self.assertTrue(second.exists())
        self.assertEqual(self.store.task_status()["stats"]["recorded_takes"],1)
        self.assertTrue(self.store.task_status()["active"])
        self.assertEqual(self.store.current()["take_id"],2)
        self.assertEqual((await self.delete_take(1,first.name)).status,404)

    async def test_deleting_highest_or_all_never_reuses_a_take_number(self):
        first=self.record();second=self.record()
        self.assertEqual((await self.delete_take(2,second.name)).status,200)
        self.assertEqual(self.store.current()["take_id"],1)
        self.assertEqual((await self.delete_take(1,first.name)).status,200)
        self.assertIsNone(self.store.current())
        self.assertEqual(self.store.task_status()["stats"]["recorded_takes"],0)
        self.assertEqual(self.store.task_status()["stats"]["next_take_id"],3)
        self.store.end_task();self.store.activate_task("builtin")
        restored=SessionTakeStore(self.root / "results",task_catalog=self.catalog)
        self.assertEqual(restored.reserve_take()["take_id"],3)

    async def test_single_delete_of_inactive_task_keeps_selected_task(self):
        bag=self.record()
        self.store.create_task({"id":"other","name":"Other","instruction":"Keep"})
        self.store.activate_task("other")
        before=self.store.task_status()
        self.assertEqual((await self.delete_take(1,bag.name)).status,200)
        self.assertEqual(self.store.task_status(),before)

    async def test_single_delete_rejects_stale_identity_and_unsafe_paths(self):
        bag=self.record();meta=self.sessions / "builtin/takes/take_0001.json"
        saved=meta.read_bytes()
        self.assertEqual((await self.delete_take(1,"different")).status,409)
        self.assertEqual(meta.read_bytes(),saved);self.assertTrue(bag.exists())
        bag.rename(bag.with_name("kept"))
        outside=self.root / "outside";outside.mkdir();(outside / "keep").write_text("keep")
        bag.symlink_to(outside,target_is_directory=True)
        self.assertEqual((await self.delete_take(1,bag.name)).status,409)
        self.assertTrue((outside / "keep").exists());self.assertEqual(meta.read_bytes(),saved)

    async def test_single_delete_rejects_shared_directory(self):
        bag=self.record();self.record()
        meta=self.sessions / "builtin/takes/take_0002.json"
        data=json.loads(meta.read_text());data["bag_path"]=str(bag);meta.write_text(json.dumps(data))
        self.assertEqual((await self.delete_take(1,bag.name)).status,409)
        self.assertTrue(bag.exists())

    async def test_failed_take_deletes_owned_recovery_and_staging_only(self):
        bag=self.record()
        meta=self.sessions / "builtin/takes/take_0001.json"
        data=json.loads(meta.read_text());data["state"]="finalization_failed";meta.write_text(json.dumps(data))
        recovered=bag.with_name(bag.name+"_recovered");recovered.mkdir()
        archive=self.raw / "_staging" / ("."+bag.name+".original");archive.mkdir(parents=True)
        (archive / "raw.mcap").write_bytes(b"original")
        (recovered / "recording_manifest.json").write_text(json.dumps({"recovered":True,"bag_name":recovered.name,"recovery_source":str(archive)}))
        staging=self.raw / "_staging" / bag.name;staging.mkdir()
        (staging / ".recording_target.json").write_text(json.dumps({"output_subdirectory":"builtin"}))
        sentinel=self.raw / "_staging" / "unrelated";sentinel.mkdir()
        self.assertEqual((await self.delete_take(1,bag.name)).status,200)
        for path in [bag,recovered,archive,staging,meta]:self.assertFalse(path.exists(),path)
        self.assertTrue(sentinel.exists())

    async def test_single_failed_start_without_bag_can_be_removed(self):
        take=self.store.reserve_take();self.store.fail_start("fixture")
        self.assertEqual((await self.delete_take(take["take_id"],take["bag_name"])).status,200)
        self.assertEqual(self.store.task_status()["stats"]["next_take_id"],2)

    async def test_single_delete_blocks_busy_capture_and_in_use_playback(self):
        first=self.record();second=self.record()
        current={"bag_name":"selected","state":"playing"}
        self.context.prepared_playback_manager=SimpleNamespace(status=lambda:current,stop=lambda:current.update(state="idle"))
        self.context.bag_library=SimpleNamespace(resolve=lambda name:first)
        self.assertEqual((await self.delete_take(1,first.name)).status,409)
        self.assertTrue(first.exists())
        self.assertEqual((await self.delete_take(2,second.name)).status,200)
        self.assertEqual(current["state"],"playing")
        self.manager.status=lambda:{"recording":True}
        self.assertEqual((await self.delete_take(1,first.name)).status,409)
        self.manager.status=lambda:{"recording":False}
        current["state"]="ready"
        self.assertEqual((await self.delete_take(1,first.name)).status,200)
        self.assertEqual(current["state"],"idle")

    async def test_single_delete_failure_is_visible_and_retryable(self):
        bag=self.record();meta=self.sessions / "builtin/takes/take_0001.json"
        with patch("insight_capture.runtime.take.shutil.rmtree",side_effect=OSError("fixture I/O error")):
            self.assertEqual((await self.delete_take(1,bag.name)).status,500)
        self.assertTrue(meta.exists());self.assertTrue(bag.exists())
        self.assertEqual((await self.delete_take(1,bag.name)).status,200)
        self.assertEqual(self.store.task_status()["stats"]["next_take_id"],2)

    async def test_single_cancelled_delete_keeps_the_admission_lock(self):
        bag=self.record();entered=threading.Event();finish=threading.Event();original=self.store.delete_take
        def slow(*args,**kwargs):entered.set();finish.wait(3);return original(*args,**kwargs)
        with patch.object(self.store,"delete_take",slow):
            request=asyncio.create_task(self.delete_take(1,bag.name))
            self.assertTrue(await asyncio.to_thread(entered.wait,2));request.cancel();await asyncio.sleep(.02)
            self.assertTrue(capture_transition_lock(self.context).locked());self.assertFalse(request.done())
            finish.set()
            with self.assertRaises(asyncio.CancelledError):await request
        self.assertFalse(capture_transition_lock(self.context).locked());self.assertFalse(bag.exists())

    async def test_single_delete_rejects_normalized_path_outside_storage(self):
        bag=self.record();meta=self.sessions / "builtin/takes/take_0001.json"
        outside=self.root / "outside" / bag.name;outside.mkdir(parents=True)
        (outside / "keep").write_text("keep")
        data=json.loads(meta.read_text());data["bag_path"]=str(self.raw / ".." / "outside" / bag.name);meta.write_text(json.dumps(data))
        self.assertEqual((await self.delete_take(1,bag.name)).status,409)
        self.assertTrue((outside / "keep").exists());self.assertTrue(meta.exists());self.assertTrue(bag.exists())
