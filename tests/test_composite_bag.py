from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from post_processing_core.bag_catalog import list_rosbags  # noqa: E402
from post_processing_core.composite_bag import (  # noqa: E402
    COMPOSITE_FORMAT,
    aggregate_metadata,
    recorded_topics,
    session_parts,
)
from post_processing_core.config import resolve_recording_root  # noqa: E402
from post_processing_core.playback import PlaybackManager  # noqa: E402
from post_processing_core.recording import RecordingManager  # noqa: E402


def _write_part(
    root: Path, name: str, topic: str, *, start_ns: int, duration_ns: int,
    messages: int,
) -> Path:
    part = root / name
    part.mkdir(parents=True)
    (part / f"{name}_0.mcap").write_bytes(b"mcap")
    info = {
        "storage_identifier": "mcap",
        "starting_time": {"nanoseconds_since_epoch": start_ns},
        "duration": {"nanoseconds": duration_ns},
        "message_count": messages,
        "topics_with_message_count": [{
            "topic_metadata": {
                "name": topic,
                "type": "std_msgs/msg/String",
                "serialization_format": "cdr",
                "offered_qos_profiles": "",
            },
            "message_count": messages,
        }],
    }
    (part / "metadata.yaml").write_text(
        yaml.safe_dump({"rosbag2_bagfile_information": info}), encoding="utf-8"
    )
    return part


def _write_manifest(root: Path, parts: list[Path]) -> None:
    (root / "recording_manifest.json").write_text(json.dumps({
        "version": 2,
        "format": COMPOSITE_FORMAT,
        "parts": [{"path": part.name} for part in parts],
    }), encoding="utf-8")


class CompositeBagTest(unittest.TestCase):
    def test_recording_waits_for_writer_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = RecordingManager({}, 20, Path(directory), 1024, ["/camera"])
            manager.merge_state = "finalizing"

            with self.assertRaisesRegex(RuntimeError, "still finalizing"):
                manager.start()

    def test_recording_falls_back_when_usb_mount_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            primary = project_root / "usb"
            primary.mkdir()
            findmnt = mock.Mock(stdout="/dev/nvme0n1p1[/media/nvidia/INSIGHT_USB/rosbags]\n")
            with mock.patch.dict(
                "os.environ",
                {
                    "INSIGHT_ROSBAG_REQUIRED_SOURCE": "/dev/sda1",
                    "INSIGHT_ROSBAG_FALLBACK_DIR": "nvme_bags",
                },
            ), mock.patch(
                "post_processing_core.config.subprocess.run", return_value=findmnt
            ):
                active, status = resolve_recording_root(primary, project_root)

            self.assertEqual(active, (project_root / "nvme_bags").resolve())
            self.assertTrue(status["using_fallback"])
            self.assertEqual(status["mounted_source"], findmnt.stdout.strip())

    def test_recording_keeps_usb_when_required_mount_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            primary = project_root / "usb"
            primary.mkdir()
            findmnt = mock.Mock(stdout="/dev/sda1[/rosbags]\n")
            with mock.patch.dict(
                "os.environ", {"INSIGHT_ROSBAG_REQUIRED_SOURCE": "/dev/sda1"}
            ), mock.patch(
                "post_processing_core.config.subprocess.run", return_value=findmnt
            ):
                active, status = resolve_recording_root(primary, project_root)

            self.assertEqual(active, primary.resolve())
            self.assertFalse(status["using_fallback"])

    def test_aggregates_parts_and_catalogs_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bag = root / "capture"
            bag.mkdir()
            parts = [
                _write_part(bag, "camera", "/camera", start_ns=1_000, duration_ns=500, messages=5),
                _write_part(bag, "auxiliary", "/imu", start_ns=900, duration_ns=800, messages=20),
            ]
            _write_manifest(bag, parts)

            self.assertEqual(session_parts(bag), parts)
            self.assertEqual(recorded_topics(bag), ["/camera", "/imu"])
            metadata = aggregate_metadata(bag)
            self.assertEqual(metadata["message_count"], 25)
            self.assertEqual(metadata["duration"]["nanoseconds"], 800)

            entries = list_rosbags(root, root / "results")
            self.assertEqual(entries[0]["name"], "capture")
            self.assertEqual(entries[0]["topic_count"], 2)
            self.assertEqual(entries[0]["message_count"], 25)

    def test_playback_starts_one_process_per_relevant_part(self) -> None:
        class Recording:
            _lock = threading.Lock()
            processes = []

            @staticmethod
            def _cleanup_if_exited_unlocked() -> None:
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bag = root / "capture"
            bag.mkdir()
            parts = [
                _write_part(bag, "camera", "/camera", start_ns=1_000_000_000, duration_ns=500, messages=5),
                _write_part(bag, "auxiliary", "/pose", start_ns=1_250_000_000, duration_ns=500, messages=20),
            ]
            _write_manifest(bag, parts)
            processes = [mock.Mock(), mock.Mock()]
            for process in processes:
                process.poll.return_value = None
            with mock.patch(
                "post_processing_core.playback.subprocess.Popen", side_effect=processes
            ) as popen, mock.patch("post_processing_core.playback.threading.Thread.start"):
                PlaybackManager(root, 20).start(
                    "capture",
                    Recording(),
                    remap_topics={"/camera": "/bagplay/camera", "/pose": "/bagplay/pose"},
                )

            self.assertEqual(popen.call_count, 2)
            first = popen.call_args_list[0].args[0]
            second = popen.call_args_list[1].args[0]
            self.assertIn("1.000000", first)
            self.assertIn("1.250000", second)
            self.assertIn("/camera", first)
            self.assertIn("/pose", second)

    def test_publish_is_atomic_and_keeps_parts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "_staging" / "capture"
            staging.mkdir(parents=True)
            _write_part(staging, "camera", "/camera", start_ns=1_000, duration_ns=500, messages=5)
            output = root / "capture"
            manager = object.__new__(RecordingManager)
            manager.storage_id = "mcap"
            manager._recording_manifest = {
                "version": 2, "format": COMPOSITE_FORMAT, "selected_topics": ["/camera"]
            }
            manager._image_header_audit = None
            manager._network_audit = None
            manager._lock = threading.Lock()
            manager._staging_dir = staging
            manager.merge_state = "finalizing"
            manager.merge_timings = {}
            manager._output_lines = deque(maxlen=20)
            manager._recording_completed_callbacks = []

            manager._publish_composite_session(staging, output)

            self.assertFalse(staging.exists())
            self.assertTrue((output / "camera/camera_0.mcap").is_file())
            manifest = json.loads((output / "recording_manifest.json").read_text())
            self.assertEqual(manifest["parts"][0]["path"], "camera")
            self.assertEqual(manager.merge_state, "done")
            self.assertEqual(manager.merge_timings["method"], "composite_publish")


if __name__ == "__main__":
    unittest.main()
