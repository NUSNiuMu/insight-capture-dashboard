import json
import tempfile
import unittest
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from insight_capture.aruco_capture import lerobot_view

STATE = {"dtype": "float32", "shape": [2], "names": ["a", "b"]}
VIDEO = {"dtype": "video", "shape": [4, 4, 3]}


def write_v3(root):
    (root / "meta/episodes/chunk-000").mkdir(parents=True)
    (root / "data/chunk-000").mkdir(parents=True)
    (root / "videos/observation.images.head/chunk-000").mkdir(parents=True)
    (root / "videos/observation.images.head/chunk-000/file-000.mp4").write_bytes(b"mp4")
    (root / "meta/info.json").write_text(json.dumps({
        "codebase_version": "v3.0", "fps": 10, "total_frames": 5,
        "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
        "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
        "features": {"observation.state": STATE, "action": STATE, "observation.images.head": VIDEO},
    }))
    pq.write_table(pa.table({"task_index": [0, 1]}, ).append_column("task", pa.array(["pick", "place"])),
                   root / "meta/tasks.parquet")
    pq.write_table(pa.Table.from_pylist([
        {"episode_index": 0, "length": 2, "tasks": ["pick"], "subtask": "grab cup",
         "videos/observation.images.head/from_timestamp": 0.0, "videos/observation.images.head/to_timestamp": 0.2},
        {"episode_index": 1, "length": 3, "tasks": ["place"], "subtask": "",
         "videos/observation.images.head/from_timestamp": 0.2, "videos/observation.images.head/to_timestamp": 0.5},
    ]), root / "meta/episodes/chunk-000/file-000.parquet")
    pq.write_table(pa.table({
        "observation.state": [[0, 0], [1, 1], [2, 2], [3, 3], [4, 4]],
        "action": [[1, 1], [2, 2], [3, 3], [4, 4], [5, 5]],
        "timestamp": [0.0, 0.1, 0.2, 0.3, 0.4],
        "frame_index": [0, 1, 0, 1, 2],
        "episode_index": [0, 0, 1, 1, 1],
        "task_index": [0, 0, 1, 1, 1],
    }), root / "data/chunk-000/file-000.parquet")


def write_v2(root):
    (root / "meta").mkdir(parents=True)
    (root / "data/chunk-000").mkdir(parents=True)
    (root / "videos/chunk-000/observation.images.top").mkdir(parents=True)
    (root / "videos/chunk-000/observation.images.top/episode_000000.mp4").write_bytes(b"mp4")
    (root / "meta/info.json").write_text(json.dumps({
        "codebase_version": "v2.1", "fps": 20, "chunks_size": 1000,
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
        "features": {"observation.state": STATE, "observation.images.top": VIDEO},
    }))
    (root / "meta/episodes.jsonl").write_text(json.dumps({"episode_index": 0, "tasks": ["fold"], "length": 2}) + "\n")
    (root / "meta/tasks.jsonl").write_text(json.dumps({"task_index": 0, "task": "fold"}) + "\n")
    pq.write_table(pa.table({
        "observation.state": [[0, 1], [2, 3]], "timestamp": [0.0, 0.05],
        "frame_index": [0, 1], "episode_index": [0, 0],
    }), root / "data/chunk-000/episode_000000.parquet")


class LerobotViewTests(unittest.TestCase):
    def test_v3_summary_and_episode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ds"
            write_v3(root)
            path = lerobot_view.resolve(tmp, "ds")
            summary = lerobot_view.summary(path)
            self.assertEqual(summary["fps"], 10)
            self.assertEqual(summary["video_keys"], ["observation.images.head"])
            self.assertEqual([e["length"] for e in summary["episodes"]], [2, 3])
            self.assertEqual(summary["episodes"][0]["subtask"], "grab cup")
            self.assertEqual(summary["tasks"], {"0": "pick", "1": "place"})
            episode = lerobot_view.episode(path, 1)
            self.assertEqual([r["observation.state"] for r in episode["rows"]], [[2, 2], [3, 3], [4, 4]])
            self.assertEqual(episode["rows"][0]["t"], 0.0)   # relative to the episode start
            video = episode["videos"]["observation.images.head"]
            self.assertEqual((video["from"], video["to"]), (0.2, 0.5))
            self.assertTrue(lerobot_view.video_file(path, video["file"]).is_file())

    def test_v2_episode_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "old"
            write_v2(root)
            path = lerobot_view.resolve(tmp, str(root))   # absolute path works too
            self.assertEqual(lerobot_view.summary(path)["episodes"][0]["tasks"], ["fold"])
            episode = lerobot_view.episode(path, 0)
            self.assertEqual(len(episode["rows"]), 2)
            self.assertEqual(episode["videos"]["observation.images.top"]["from"], 0.0)

    def test_rejects_non_dataset_and_escaping_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                lerobot_view.resolve(tmp, "missing")
            root = Path(tmp) / "ds"
            write_v3(root)
            (Path(tmp) / "secret.mp4").write_bytes(b"x")
            with self.assertRaises(ValueError):
                lerobot_view.video_file(root, "../secret.mp4")
            with self.assertRaises(ValueError):
                lerobot_view.video_file(root, "meta/info.json")


if __name__ == "__main__":
    unittest.main()


class BrowseTests(unittest.TestCase):
    def test_lists_folders_marks_datasets_and_stays_inside_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_v3(root / "data" / "ds")
            (root / "data" / "plain").mkdir()
            (root / "data" / ".hidden").mkdir()
            (root / "data" / "file.txt").write_text("x")
            top = lerobot_view.browse("", [root])
            self.assertEqual([e["path"] for e in top["entries"]], [str(root.resolve())])
            listing = lerobot_view.browse(str(root / "data"), [root])
            self.assertEqual([e["name"] for e in listing["entries"]], ["ds", "plain"])
            self.assertEqual(listing["entries"][0]["dataset"]["version"], "v3.0")
            self.assertIsNone(listing["entries"][1]["dataset"])
            self.assertEqual(listing["parent"], str(root.resolve()))
            self.assertEqual(lerobot_view.browse(str(root), [root])["parent"], "")   # a root: back to the roots
            with self.assertRaises(ValueError):
                lerobot_view.browse(str(root.parent), [root])
