from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from insight_capture.postprocess.bags.playback import PlaybackManager  # noqa: E402


class _RecordingManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.processes = []

    def _cleanup_if_exited_unlocked(self) -> None:
        return None


class PlaybackManagerTest(unittest.TestCase):
    def test_start_replays_only_dashboard_topics_present_in_bag(self) -> None:
        metadata = """\
rosbag2_bagfile_information:
  topics_with_message_count:
    - topic_metadata:
        name: /camera
    - topic_metadata:
        name: /pose
    - topic_metadata:
        name: /unused/imu
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bag_path = root / "sample"
            bag_path.mkdir()
            (bag_path / "metadata.yaml").write_text(metadata, encoding="utf-8")
            manager = PlaybackManager(root, 20)
            process = mock.Mock()
            process.wait.return_value = 0
            with mock.patch(
                "insight_capture.postprocess.bags.playback.subprocess.Popen", return_value=process
            ) as popen:
                manager.start(
                    "sample",
                    _RecordingManager(),
                    remap_topics={
                        "/camera": "/bagplay/camera",
                        "/pose": "/bagplay/pose",
                        "/missing": "/bagplay/missing",
                    },
                )

        command = popen.call_args.args[0]
        topics_index = command.index("--topics")
        remap_index = command.index("--remap")
        self.assertEqual(command[topics_index + 1:remap_index], ["/camera", "/pose"])
        self.assertNotIn("/unused/imu", command)


if __name__ == "__main__":
    unittest.main()
