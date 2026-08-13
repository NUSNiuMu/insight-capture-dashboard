from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from dashboard_runtime.voice_recording import (  # noqa: E402
    VoiceRecordingController,
    next_restart_backoff,
)
from voice_control_worker import (  # noqa: E402
    build_arecord_command,
    build_phrase_map,
    classify_transcript,
    segment_phrase_for_model,
)


class _RecordingManager:
    def __init__(self, root: Path) -> None:
        self.rosbag_root = root
        self.merge_state = "idle"
        self.recording = False
        self.output_path = None
        self.starts = []
        self.stop_count = 0

    def status(self):
        return {"recording": self.recording, "output_path": self.output_path}

    def start(self, topics=None, bag_name=None):
        self.starts.append((topics, bag_name))
        self.recording = True
        self.output_path = str(self.rosbag_root / str(bag_name))
        return self.status()

    def stop(self):
        self.stop_count += 1
        self.recording = False
        return self.status()


class VoiceControlWorkerTest(unittest.TestCase):
    def test_restart_backoff_grows_to_cap_and_resets_after_stable_run(self) -> None:
        self.assertEqual(next_restart_backoff(2.0, 1.0, 2.0, 60.0, 30.0), 4.0)
        self.assertEqual(next_restart_backoff(32.0, 1.0, 2.0, 60.0, 30.0), 60.0)
        self.assertEqual(next_restart_backoff(60.0, 30.0, 2.0, 60.0, 30.0), 2.0)

    def test_transcript_matching_is_exact_after_spacing_and_punctuation(self) -> None:
        phrase_map = build_phrase_map(["开始录制"], ["结束录制", "停止录制"])

        self.assertEqual(classify_transcript("开 始 录 制。", phrase_map), "start")
        self.assertEqual(classify_transcript("停止录制", phrase_map), "stop")
        self.assertIsNone(classify_transcript("可以开始录制吗", phrase_map))

    def test_arecord_uses_plug_device_and_raw_mono_audio(self) -> None:
        command = build_arecord_command("plughw:YDPI4MIC,0", 16000)

        self.assertEqual(command[:4], ["arecord", "-q", "-D", "plughw:YDPI4MIC,0"])
        self.assertIn("raw", command)
        self.assertEqual(command[command.index("-c") + 1], "1")

    def test_phrase_is_segmented_to_words_present_in_model(self) -> None:
        class _Model:
            words = {"开始", "录制", "结束", "停止"}

            def vosk_model_find_word(self, word):
                return 1 if word in self.words else -1

        self.assertEqual(segment_phrase_for_model("开始录制", _Model()), "开始 录制")
        self.assertEqual(segment_phrase_for_model("结束录制", _Model()), "结束 录制")


class VoiceRecordingControllerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.manager = _RecordingManager(self.root)
        self.controller = VoiceRecordingController(
            self.manager,
            {"enabled": False, "min_free_ratio": 0.0},
            self.root,
            lambda _message: None,
        )
        # Exercise recording policy without spawning the recognizer process.
        self.controller.enabled = True

    def tearDown(self) -> None:
        self.controller.close()
        self.directory.cleanup()

    def test_voice_started_recording_can_be_stopped_by_voice(self) -> None:
        self.controller._apply_recording_command("start", "开始录制")

        self.assertTrue(self.manager.recording)
        self.assertEqual(len(self.manager.starts), 1)
        self.assertRegex(self.manager.starts[0][1], r"^voice_record_\d{8}_\d{6}$")
        self.assertEqual(self.controller.status()["state"], "recording")

        self.controller._apply_recording_command("stop", "结束录制")

        self.assertFalse(self.manager.recording)
        self.assertEqual(self.manager.stop_count, 1)
        self.assertEqual(self.controller.status()["last_event"], "stopped")

    def test_voice_does_not_stop_manual_recording(self) -> None:
        self.manager.recording = True
        self.manager.output_path = str(self.root / "manual_record")

        self.controller._apply_recording_command("stop", "结束录制")

        self.assertTrue(self.manager.recording)
        self.assertEqual(self.manager.stop_count, 0)
        self.assertEqual(
            self.controller.status()["last_event"],
            "ignored_manual_recording",
        )


if __name__ == "__main__":
    unittest.main()
