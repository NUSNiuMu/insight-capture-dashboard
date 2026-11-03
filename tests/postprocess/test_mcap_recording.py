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


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from insight_capture.core.bag import (  # noqa: E402
    recorded_topics,
    single_mcap,
)
from insight_capture.runtime.recording.storage import (  # noqa: E402
    probe_recording_root,
    resolve_recording_root,
)
from insight_capture.postprocess.bags.playback import PlaybackManager  # noqa: E402
from insight_capture.runtime.recording.manager import RecordingManager  # noqa: E402
from insight_capture.runtime.recording.header_audit import RecordingBridge  # noqa: E402


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


class McapRecordingTest(unittest.TestCase):
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
                "insight_capture.runtime.recording.manager.subprocess.Popen", return_value=process
            ) as popen, mock.patch("insight_capture.runtime.recording.manager.os.write"):
                result = manager.start(
                    bag_name="single", output_subdirectory="cup_stacking"
                )

            self.assertEqual(set(calls[0]), {"/camera"})
            self.assertEqual(len(set(calls[0].values())), 1)
            popen.assert_called_once()
            command = popen.call_args.args[0]
            self.assertEqual(command[command.index("--output") + 1], calls[0]["/camera"])
            self.assertIn("/camera", command)
            self.assertIn("/imu", command)
            self.assertIn("--start-paused", command)
            self.assertEqual(
                result["output_path"],
                str(Path(directory) / "cup_stacking" / "single"),
            )
            target = Path(command[command.index("--output") + 1])
            self.assertEqual(
                json.loads((target / ".recording_target.json").read_text()),
                {"output_subdirectory": "cup_stacking"},
            )
            self.assertEqual(
                popen.call_args.kwargs["env"]["RMW_IMPLEMENTATION"],
                "rmw_cyclonedds_cpp",
            )
            timings = result["start_timings"]
            self.assertGreaterEqual(timings["total_sec"], 0.0)
            self.assertLessEqual(
                timings["resume_requested_offset_sec"],
                timings["resume_confirmed_offset_sec"],
            )
            self.assertIn("dds_subscription_settle_sec", timings)

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
                "insight_capture.runtime.recording.storage.subprocess.run", return_value=findmnt
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
                "insight_capture.runtime.recording.storage.subprocess.run", return_value=findmnt
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
                "insight_capture.runtime.recording.storage.subprocess.run", return_value=findmnt
            ), mock.patch(
                "insight_capture.runtime.recording.storage.probe_recording_root", side_effect=probe
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
                "insight_capture.runtime.recording.manager.subprocess.Popen", return_value=process
            ) as popen, mock.patch("insight_capture.runtime.recording.manager.os.write"):
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

            manager._publish_single_mcap_session(staging, output)

            self.assertFalse(staging.exists())
            self.assertTrue((output / "capture_0.mcap").is_file())
            self.assertTrue((output / "metadata.yaml").is_file())
            self.assertEqual(single_mcap(output), output / "capture_0.mcap")
            manifest = json.loads((output / "recording_manifest.json").read_text())
            self.assertNotIn("parts", manifest)
            self.assertEqual(manager.merge_timings["method"], "single_mcap_publish")


if __name__ == "__main__":
    unittest.main()
