import io
import json
import sys
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from openclaw_voice_bridge import (  # noqa: E402
    OpenClawVoiceBridge,
    build_agent_command,
    calibration_is_complete,
    clean_utterance_transcript,
    extract_openclaw_reply,
    match_local_command,
    normalize_transcript,
    speech_text,
    strip_wake_prefix,
    wake_tone_wav,
    wake_word_detected,
    parse_args,
)


class OpenClawVoiceBridgeTest(unittest.TestCase):
    def test_normalize_and_match_wake_word(self):
        self.assertEqual(normalize_transcript("  宸境！ "), "宸境")
        self.assertTrue(wake_word_detected("宸境", ["宸境"]))
        self.assertTrue(wake_word_detected("澄净。", ["宸境", "澄净"]))
        self.assertTrue(wake_word_detected("成静。", ["宸境", "成静"]))
        self.assertTrue(wake_word_detected("沉静。", ["宸境", "沉静"]))
        self.assertTrue(wake_word_detected("很静。", ["宸境", "很静"]))
        self.assertTrue(wake_word_detected("南静。", ["宸境", "南静"]))
        self.assertTrue(wake_word_detected("沉浸。", ["宸境", "沉浸"]))
        self.assertFalse(wake_word_detected("我喜欢沉浸式体验", ["宸境", "沉浸"]))
        self.assertFalse(wake_word_detected("我曾经去过", ["宸境", "澄净"]))

    def test_local_command_matching_is_exact_and_accepts_polite_forms(self):
        self.assertEqual(match_local_command("开始录制。"), "recording_start")
        self.assertEqual(match_local_command("请停止录制"), "recording_stop")
        self.assertEqual(match_local_command("帮我重新校准一下"), "calibration_start")
        self.assertIsNone(match_local_command("开始录制前先检查磁盘"))
        self.assertIsNone(match_local_command("现在是否正在录制"))

    def test_local_command_bypasses_openclaw(self):
        bridge = OpenClawVoiceBridge(SimpleNamespace())
        with (
            mock.patch.object(
                bridge, "execute_local_command", return_value="recording_started"
            ) as execute,
            mock.patch.object(bridge, "speak_canned") as speak,
            mock.patch.object(bridge, "ask_openclaw") as ask_openclaw,
        ):
            bridge.handle_utterance("开始录制")
        execute.assert_called_once_with("recording_start")
        speak.assert_called_once_with("recording_started")
        ask_openclaw.assert_not_called()

    def test_wake_followup_forces_openclaw_even_for_fixed_command(self):
        bridge = OpenClawVoiceBridge(SimpleNamespace())
        with (
            mock.patch.object(bridge, "execute_local_command") as execute,
            mock.patch.object(bridge, "ask_openclaw", return_value="好的。") as ask,
            mock.patch.object(bridge, "speak") as speak,
        ):
            bridge.handle_utterance("开始录制", allow_local_commands=False)
        execute.assert_not_called()
        ask.assert_called_once_with("开始录制")
        speak.assert_called_once_with("好的。")

    def test_local_command_calls_dashboard_automation_endpoint(self):
        bridge = OpenClawVoiceBridge(
            SimpleNamespace(
                dashboard_url="http://127.0.0.1:8765/",
                dashboard_timeout_sec=7.0,
            )
        )
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"recording": true}'
        with mock.patch("urllib.request.urlopen", return_value=response) as urlopen:
            reply_key = bridge.execute_local_command("recording_start")
        self.assertEqual(reply_key, "recording_started")
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:8765/api/automation/recording/start",
        )
        self.assertEqual(request.method, "POST")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 7.0)

    def test_calibration_command_calls_mapping_reset(self):
        bridge = OpenClawVoiceBridge(
            SimpleNamespace(
                dashboard_url="http://127.0.0.1:8765",
                dashboard_timeout_sec=7.0,
            )
        )
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok": true}'
        with (
            mock.patch("urllib.request.urlopen", return_value=response) as urlopen,
            mock.patch.object(bridge, "_start_calibration_monitor") as monitor,
        ):
            reply_key = bridge.execute_local_command("calibration_start")
        self.assertEqual(reply_key, "calibration_started")
        monitor.assert_called_once_with()
        self.assertEqual(
            urlopen.call_args.args[0].full_url,
            "http://127.0.0.1:8765/api/mapping/reset",
        )

    def test_calibration_completion_requires_both_insight3_devices(self):
        self.assertFalse(calibration_is_complete({"statuses": {}}))
        self.assertFalse(
            calibration_is_complete(
                {
                    "statuses": {
                        "insight3_a": {"localized": True},
                        "insight3_b": {"localized": False},
                    }
                }
            )
        )
        self.assertTrue(
            calibration_is_complete(
                {
                    "statuses": {
                        "insight3_a": {"localized": True},
                        "insight3_b": {"localized": True},
                    }
                }
            )
        )

    def test_extract_reply_prefers_visible_final_text(self):
        payload = {
            "payloads": [{"text": "fallback"}],
            "meta": {"finalAssistantVisibleText": "录制已开始。"},
        }
        self.assertEqual(extract_openclaw_reply(payload), "录制已开始。")

    def test_extract_reply_accepts_payload_list(self):
        self.assertEqual(
            extract_openclaw_reply({"payloads": [{"text": "第一句。"}, {"text": "第二句。"}]}),
            "第一句。\n第二句。",
        )

    def test_extract_reply_accepts_gateway_result_wrapper(self):
        payload = {
            "status": "ok",
            "result": {
                "payloads": [{"text": "我是宸境。"}],
                "meta": {"finalAssistantVisibleText": "我是宸境。"},
            },
        }
        self.assertEqual(extract_openclaw_reply(payload), "我是宸境。")

    def test_speech_text_removes_markdown_and_caps_length(self):
        self.assertEqual(speech_text("**查看** [页面](http://localhost)"), "查看 页面")
        self.assertEqual(speech_text("一" * 8, max_chars=5), "一一一一。")

    def test_clean_utterance_repairs_domain_phrase(self):
        self.assertEqual(clean_utterance_transcript("检查 一下 素材 状态"), "检查一下数采状态")
        self.assertEqual(clean_utterance_transcript("检查一下素菜状态。"), "检查一下数采状态。")
        self.assertEqual(clean_utterance_transcript("开始 录制。"), "开始录制。")

    def test_strip_wake_prefix_preserves_command(self):
        self.assertEqual(strip_wake_prefix("宸境，检查数采状态。", ["宸境"]), "检查数采状态。")
        self.assertEqual(strip_wake_prefix("检查宸境状态", ["宸境"]), "检查宸境状态")

    def test_wake_tone_is_valid_mono_wav(self):
        payload = wake_tone_wav(16000, 140, 880, 0.3)
        with wave.open(io.BytesIO(payload), "rb") as audio:
            self.assertEqual(audio.getnchannels(), 1)
            self.assertEqual(audio.getframerate(), 16000)
            self.assertEqual(audio.getnframes(), 2240)

    def test_agent_command_keeps_utterance_in_single_argument(self):
        command = build_agent_command(Path("/openclaw"), "voice", "开始录制", 90)
        self.assertEqual(command[:4], ["/openclaw", "agent", "--session-key", "voice"])
        self.assertIn("openai/gpt-5.6-luna", command)
        self.assertIn("用户说：开始录制", command[7])
        self.assertIn("off", command)
        self.assertEqual(command[-3:], ["--timeout", "90", "--json"])
        self.assertEqual(json.loads(json.dumps(command)), command)

    def test_voice_defaults_use_two_stage_fast_interaction(self):
        with mock.patch.object(sys, "argv", ["openclaw_voice_bridge.py"]):
            args = parse_args()
        self.assertEqual(args.wake_pause_sec, 0.5)
        self.assertEqual(args.wake_phrase, ["宸境"])
        self.assertIn("澄净", args.wake_alias)
        self.assertIn("成静", args.wake_alias)
        self.assertIn("沉静", args.wake_alias)
        self.assertIn("沉浸", args.wake_alias)
        self.assertNotIn("曾经", args.wake_alias)
        self.assertEqual(args.wake_feedback, "speech")
        self.assertEqual(args.agent_thinking, "off")


if __name__ == "__main__":
    unittest.main()
