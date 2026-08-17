from __future__ import annotations

from collections import deque
import io
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
from post_processing_core.config import (  # noqa: E402
    probe_recording_root,
    resolve_recording_root,
)
from post_processing_core.playback import PlaybackManager  # noqa: E402
from post_processing_core.recording import RecordingManager  # noqa: E402
from dashboard_runtime.recording_bridge import RecordingBridge  # noqa: E402


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
    def test_live_audit_republishes_latched_tf_after_recorder_resume(self) -> None:
        owner = mock.Mock()
        owner._recording_writer_lock = threading.Lock()
        owner._recording_writer_by_topic = {}
        owner._recording_header_audit = {}

        RecordingBridge(owner).start_image_recording({"/camera": "/bag"})

        owner.republish_tf_static.assert_called_once_with()
        self.assertIn("/camera", owner._recording_header_audit)

    def test_recorder_output_distinguishes_actual_cache_loss_from_qos_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = RecordingManager({}, 20, Path(directory), 1024, ["/camera"])
            process = mock.Mock(stdout=io.StringIO(
                "Some messages from Reliable publishers could be dropped.\n"
                "Cache buffers lost messages per topic: /camera=3\n"
            ))

            manager._drain_stdout("single", process)

            self.assertEqual(
                manager._recorder_loss_lines,
                ["Cache buffers lost messages per topic: /camera=3"],
            )

    def test_native_recorder_receives_every_topic_on_one_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calls = []

            def start_writer(topic_paths: dict[str, str]) -> None:
                calls.append(topic_paths)

            manager = RecordingManager(
                {}, 20, Path(directory), 1024, ["/camera", "/imu"],
                image_topics=["/camera"],
                start_image_recording=start_writer,
                stop_image_recording=lambda: {},
            )
            process = mock.Mock(stdout=io.StringIO(
                "Waiting for recording: Press SPACE to start.\n"
                "Subscribed to topic '/camera'\n"
                "All requested topics are subscribed. Stopping discovery...\n"
                "Resuming recording.\n"
            ))
            process.poll.return_value = None
            with mock.patch(
                "post_processing_core.recording.subprocess.Popen", return_value=process
            ) as popen, mock.patch("post_processing_core.recording.os.write"):
                manager.start(bag_name="single")

            self.assertEqual(set(calls[0]), {"/camera"})
            self.assertEqual(len(set(calls[0].values())), 1)
            popen.assert_called_once()
            command = popen.call_args.args[0]
            self.assertEqual(command[command.index("--output") + 1], calls[0]["/camera"])
            self.assertIn("/camera", command)
            self.assertIn("/imu", command)
            self.assertIn("--start-paused", command)
            self.assertEqual(
                popen.call_args.kwargs["env"]["RMW_IMPLEMENTATION"],
                "rmw_cyclonedds_cpp",
            )

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

    def test_recording_falls_back_when_matching_mount_returns_io_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            primary = project_root / "usb"
            fallback = project_root / "nvme_bags"
            primary.mkdir()
            findmnt = mock.Mock(stdout="/dev/sda1[/rosbags]\n")

            def probe(path: Path):
                return "OSError: input/output error" if path == primary else None

            with mock.patch.dict(
                "os.environ",
                {
                    "INSIGHT_ROSBAG_REQUIRED_SOURCE": "/dev/sda1",
                    "INSIGHT_ROSBAG_FALLBACK_DIR": str(fallback),
                },
            ), mock.patch(
                "post_processing_core.config.subprocess.run", return_value=findmnt
            ), mock.patch(
                "post_processing_core.config.probe_recording_root", side_effect=probe
            ):
                active, status = resolve_recording_root(primary, project_root)

            self.assertEqual(active, fallback.resolve())
            self.assertTrue(status["using_fallback"])
            self.assertIn("input/output error", status["fallback_reason"])

    def test_storage_probe_writes_and_removes_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            self.assertIsNone(probe_recording_root(root))
            self.assertEqual(list((root / "_staging").iterdir()), [])

    def test_recording_refreshes_storage_before_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            primary = project_root / "usb"
            fallback = project_root / "nvme"
            primary.mkdir()
            fallback.mkdir()
            status = {
                "configured_path": str(primary),
                "active_path": str(fallback),
                "required_source": "/dev/sda1",
                "mounted_source": "/dev/sda1[/rosbags]",
                "using_fallback": True,
                "fallback_reason": "primary storage probe failed: EIO",
            }
            changed = []
            manager = RecordingManager(
                {}, 20, primary, 1024, ["/camera"],
                storage_status={"active_path": str(primary), "using_fallback": False},
                storage_resolver=lambda: (fallback, status),
            )
            manager.add_storage_changed_callback(changed.append)
            process = mock.Mock(stdout=io.StringIO(
                "Waiting for recording: Press SPACE to start.\n"
                "Subscribed to topic '/camera'\n"
                "All requested topics are subscribed. Stopping discovery...\n"
                "Resuming recording.\n"
            ))
            process.poll.return_value = None

            with mock.patch(
                "post_processing_core.recording.subprocess.Popen", return_value=process
            ) as popen, mock.patch("post_processing_core.recording.os.write"):
                result = manager.start(bag_name="fallback")

            self.assertEqual(manager.rosbag_root, fallback.resolve())
            self.assertEqual(changed, [fallback.resolve()])
            self.assertTrue(result["storage"]["using_fallback"])
            command = popen.call_args.args[0]
            recorder_output = Path(command[command.index("--output") + 1])
            self.assertTrue(recorder_output.is_relative_to(fallback.resolve()))
            self.assertEqual(
                Path(manager.output_path or "").parent,
                fallback.resolve(),
            )

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

    def test_publish_single_mcap_is_a_standard_rosbag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging_root = root / "_staging"
            staging_root.mkdir()
            staging = _write_part(
                staging_root,
                "capture",
                "/camera",
                start_ns=1_000,
                duration_ns=500,
                messages=5,
            )
            output = root / "capture"
            manager = object.__new__(RecordingManager)
            manager.storage_id = "mcap"
            manager._recording_manifest = {
                "version": 3, "format": "rosbag2", "selected_topics": ["/camera"]
            }
            manager._image_header_audit = None
            manager._network_audit = None
            manager._lock = threading.Lock()
            manager._staging_dir = staging
            manager.merge_state = "finalizing"
            manager.merge_timings = {}
            manager._output_lines = deque(maxlen=20)
            manager._recording_completed_callbacks = []

            manager._publish_recording_session(staging, output)

            self.assertFalse(staging.exists())
            self.assertTrue((output / "capture_0.mcap").is_file())
            self.assertTrue((output / "metadata.yaml").is_file())
            self.assertEqual(session_parts(output), [output])
            manifest = json.loads((output / "recording_manifest.json").read_text())
            self.assertNotIn("parts", manifest)
            self.assertEqual(manager.merge_timings["method"], "single_mcap_publish")


if __name__ == "__main__":
    unittest.main()
