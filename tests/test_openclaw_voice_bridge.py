import io
import json
import sys
import unittest
import wave
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from openclaw_voice_bridge import (  # noqa: E402
    build_agent_command,
    clean_utterance_transcript,
    extract_openclaw_reply,
    normalize_transcript,
    speech_text,
    strip_wake_prefix,
    wake_tone_wav,
    wake_word_detected,
    parse_args,
)


class OpenClawVoiceBridgeTest(unittest.TestCase):
    def test_normalize_and_match_wake_word(self):
        self.assertEqual(normalize_transcript("  Looper! "), "looper")
        self.assertTrue(wake_word_detected("looper", ["looper"]))
        self.assertFalse(wake_word_detected("blooper", ["looper"]))
        self.assertTrue(wake_word_detected("小 智", ["小智"]))

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

    def test_speech_text_removes_markdown_and_caps_length(self):
        self.assertEqual(speech_text("**查看** [页面](http://localhost)"), "查看 页面")
        self.assertEqual(speech_text("一" * 8, max_chars=5), "一一一一。")

    def test_clean_utterance_repairs_domain_phrase(self):
        self.assertEqual(clean_utterance_transcript("检查 一下 素材 状态"), "检查一下数采状态")
        self.assertEqual(clean_utterance_transcript("检查一下素菜状态。"), "检查一下数采状态。")
        self.assertEqual(clean_utterance_transcript("开始 录制。"), "开始录制。")

    def test_strip_wake_prefix_preserves_command(self):
        self.assertEqual(strip_wake_prefix("Looper，检查数采状态。", ["looper"]), "检查数采状态。")
        self.assertEqual(strip_wake_prefix("检查 Looper 状态", ["looper"]), "检查 Looper 状态")

    def test_wake_tone_is_valid_mono_wav(self):
        payload = wake_tone_wav(16000, 140, 880, 0.3)
        with wave.open(io.BytesIO(payload), "rb") as audio:
            self.assertEqual(audio.getnchannels(), 1)
            self.assertEqual(audio.getframerate(), 16000)
            self.assertEqual(audio.getnframes(), 2240)

    def test_agent_command_keeps_utterance_in_single_argument(self):
        command = build_agent_command(Path("/openclaw"), "voice", "开始录制", 90)
        self.assertEqual(command[:5], ["/openclaw", "agent", "--local", "--session-key", "voice"])
        self.assertIn("openai/gpt-5.6-luna", command)
        self.assertIn("用户说：开始录制", command[8])
        self.assertIn("off", command)
        self.assertEqual(command[-3:], ["--timeout", "90", "--json"])
        self.assertEqual(json.loads(json.dumps(command)), command)

    def test_voice_defaults_use_two_stage_fast_interaction(self):
        with mock.patch.object(sys, "argv", ["openclaw_voice_bridge.py"]):
            args = parse_args()
        self.assertEqual(args.wake_pause_sec, 0.5)
        self.assertEqual(args.wake_feedback, "speech")
        self.assertEqual(args.agent_thinking, "off")


if __name__ == "__main__":
    unittest.main()
