import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from insight_capture.api.routes.recording import RecordingRoutes  # noqa: E402
from insight_capture.postprocess.bags import BagLibrary  # noqa: E402
from insight_capture.runtime.recording.manager import RecordingManager  # noqa: E402


def _manager(root: Path, *browse_roots: Path) -> RecordingManager:
    return RecordingManager(
        raw_config={"cameras": []},
        ros_domain_id=20,
        rosbag_root=root,
        max_cache_size=1024,
        default_topics=[],
        storage_browse_roots=browse_roots or [root],
    )


class RecordingStorageTest(unittest.TestCase):
    def test_library_keeps_old_and_new_recording_directories_visible(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "rosbags"
            old_bag = root / "campaign-a" / "take-a"
            new_root = root / "campaign-b"
            new_bag = new_root / "take-b"
            for bag in (old_bag, new_bag):
                bag.mkdir(parents=True)
                (bag / "metadata.yaml").write_text(
                    "rosbag2_bagfile_information:\n  message_count: 1\n"
                )
            manager = _manager(root)
            manager.select_recording_root(str(new_root))
            library = BagLibrary(manager.storage_browse_roots, lambda: manager.rosbag_root)

            all_locations = library.locations("all")
            current_locations = library.locations("current")

            self.assertEqual(
                {item.path for item in all_locations},
                {old_bag.resolve(), new_bag.resolve()},
            )
            self.assertEqual([item.path for item in current_locations], [new_bag.resolve()])
            self.assertEqual(library.resolve(all_locations[0].bag_id), all_locations[0].path)

    def test_library_rejects_ambiguous_legacy_bag_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "rosbags"
            for campaign in ("campaign-a", "campaign-b"):
                bag = root / campaign / "take-0001"
                bag.mkdir(parents=True)
                (bag / "metadata.yaml").write_text("rosbag2_bagfile_information: {}\n")
            manager = _manager(root)
            library = BagLibrary(manager.storage_browse_roots, lambda: manager.rosbag_root)

            with self.assertRaisesRegex(ValueError, "ambiguous"):
                library.resolve("take-0001")

    def test_browser_stays_inside_allowed_roots_and_hides_bags(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "rosbags"
            destination = root / "campaign-a"
            bag = root / "insight_record_20260818_120000"
            destination.mkdir(parents=True)
            bag.mkdir()
            (bag / "metadata.yaml").write_text("rosbag2_bagfile_information: {}")
            manager = _manager(root)

            payload = manager.browse_recording_directories()

            self.assertEqual(payload["path"], str(root.resolve()))
            self.assertEqual(
                [item["name"] for item in payload["directories"]],
                ["campaign-a"],
            )
            with self.assertRaisesRegex(ValueError, "outside the allowed"):
                manager.browse_recording_directories(temporary)

    def test_selection_probes_storage_and_updates_consumers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "rosbags"
            destination = root / "campaign-b"
            destination.mkdir(parents=True)
            manager = _manager(root)
            changed = []
            manager.add_storage_changed_callback(changed.append)

            payload = manager.select_recording_root(str(destination))

            self.assertEqual(manager.rosbag_root, destination.resolve())
            self.assertEqual(payload["storage"]["active_path"], str(destination.resolve()))
            self.assertTrue(payload["storage"]["manually_selected"])
            self.assertEqual(changed, [destination.resolve()])
            self.assertTrue((destination / "_staging").is_dir())

    def test_selection_is_rejected_during_recording(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "rosbags"
            destination = root / "campaign-c"
            destination.mkdir(parents=True)
            manager = _manager(root)
            manager._image_writer_active = True

            with self.assertRaisesRegex(RuntimeError, "while recording"):
                manager.select_recording_root(str(destination))


class _Request:
    def __init__(self, *, query=None, payload=None):
        self.query = query or {}
        self._payload = payload or {}

    async def json(self):
        return self._payload

    @property
    def can_read_body(self):
        return True


class _Context:
    def __init__(self, manager):
        self.recording_manager = manager


class RecordingStorageRoutesTest(unittest.IsolatedAsyncioTestCase):
    async def test_browse_and_select_routes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "rosbags"
            destination = root / "campaign-d"
            destination.mkdir(parents=True)
            manager = _manager(root)
            routes = RecordingRoutes(_Context(manager))

            browse_response = await routes._handle_recording_directories(
                _Request(query={"path": str(root)})
            )
            select_response = await routes._handle_recording_directory_select(
                _Request(payload={"path": str(destination)})
            )

            self.assertEqual(browse_response.status, 200)
            self.assertEqual(
                json.loads(browse_response.text)["directories"][0]["name"],
                "campaign-d",
            )
            self.assertEqual(select_response.status, 200)
            self.assertEqual(
                json.loads(select_response.text)["storage"]["active_path"],
                str(destination.resolve()),
            )


if __name__ == "__main__":
    unittest.main()
