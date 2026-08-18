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
    CANNED_REPLIES,
    OpenClawVoiceBridge,
    build_agent_command,
    calibration_is_complete,
    capture_check_reply_key,
    clean_utterance_transcript,
    discover_alsa_device,
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
    def test_recording_start_uses_requested_immediate_prompt(self):
        self.assertEqual(
            CANNED_REPLIES["recording_starting"],
            "初始化录制中，请稍等。",
        )

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
        self.assertEqual(match_local_command("检查相机"), "capture_check")
        self.assertEqual(match_local_command("请设置检测位"), "capture_reference")
        self.assertEqual(match_local_command("系统状态"), "system_status")
        self.assertEqual(match_local_command("请本条作废"), "take_reject")
        self.assertIsNone(match_local_command("设置批次基准"))
        self.assertIsNone(match_local_command("开始录制前先检查磁盘"))
        self.assertIsNone(match_local_command("现在是否正在录制"))

    def test_audio_auto_discovery_prefers_stable_card_name(self):
        completed = SimpleNamespace(
            stdout="default\nplughw:CARD=Other,DEV=0\nplughw:CARD=E3,DEV=0\n"
        )
        with mock.patch("subprocess.run", return_value=completed):
            self.assertEqual(
                discover_alsa_device("capture", "E3"),
                "plughw:CARD=E3,DEV=0",
            )

    def test_missing_openclaw_only_disables_optional_requests(self):
        bridge = OpenClawVoiceBridge(SimpleNamespace(openclaw_bin=Path("/missing/openclaw")))
        with self.assertRaisesRegex(RuntimeError, "optional assistant"):
            bridge.ask_openclaw("今天采了多少条")

    def test_local_command_bypasses_openclaw(self):
        bridge = OpenClawVoiceBridge(SimpleNamespace())
        events = []

        def execute(action):
            events.append(("execute", action))
            return "recording_started"

        def speak(key):
            events.append(("speak", key))
            return True

        with (
            mock.patch.object(
                bridge, "execute_local_command", side_effect=execute
            ) as execute,
            mock.patch.object(bridge, "speak_canned", side_effect=speak),
            mock.patch.object(bridge, "ask_openclaw") as ask_openclaw,
            mock.patch.object(bridge, "_rollback_unconfirmed_recording") as rollback,
        ):
            bridge.handle_utterance("开始录制")
        execute.assert_called_once_with("recording_start")
        self.assertEqual(
            events,
            [
                ("speak", "recording_starting"),
                ("execute", "recording_start"),
                ("speak", "recording_started"),
            ],
        )
        ask_openclaw.assert_not_called()
        rollback.assert_not_called()

    def test_recording_is_rolled_back_when_start_feedback_fails(self):
        bridge = OpenClawVoiceBridge(SimpleNamespace())
        with (
            mock.patch.object(
                bridge, "execute_local_command", return_value="recording_started"
            ),
            mock.patch.object(
                bridge,
                "speak_canned",
                side_effect=[True, RuntimeError("sink busy")],
            ),
            mock.patch.object(bridge, "_rollback_unconfirmed_recording") as rollback,
        ):
            bridge.handle_utterance("开始录制")
        rollback.assert_called_once_with()

    def test_recording_is_not_started_without_immediate_feedback(self):
        bridge = OpenClawVoiceBridge(SimpleNamespace())
        with (
            mock.patch.object(bridge, "speak_canned", return_value=False) as speak,
            mock.patch.object(bridge, "execute_local_command") as execute,
        ):
            bridge.handle_utterance("开始录制")
        speak.assert_called_once_with("recording_starting")
        execute.assert_not_called()

    def test_recording_stop_announces_before_waiting_for_finalization(self):
        bridge = OpenClawVoiceBridge(SimpleNamespace())
        events = []

        def speak(key):
            events.append(("speak", key))
            return True

        def execute(action):
            events.append(("execute", action))
            return "recording_stopped"

        with (
            mock.patch.object(bridge, "speak_canned", side_effect=speak),
            mock.patch.object(bridge, "execute_local_command", side_effect=execute),
        ):
            bridge.handle_utterance("停止录制")
        self.assertEqual(
            events,
            [
                ("speak", "recording_stopping"),
                ("execute", "recording_stop"),
                ("speak", "recording_stopped"),
            ],
        )

    def test_non_recording_feedback_failure_does_not_crash_or_rollback(self):
        bridge = OpenClawVoiceBridge(SimpleNamespace())
        with (
            mock.patch.object(
                bridge, "execute_local_command", return_value="calibration_started"
            ),
            mock.patch.object(
                bridge, "speak_canned", side_effect=RuntimeError("sink busy")
            ),
            mock.patch.object(bridge, "_rollback_unconfirmed_recording") as rollback,
        ):
            bridge.handle_utterance("开始校准")
        rollback.assert_not_called()

    def test_shared_playback_uses_pulse_sink(self):
        bridge = OpenClawVoiceBridge(
            SimpleNamespace(
                playback_backend="pulse",
                pulse_sink="alsa_output.usb-E3",
                playback_device="plughw:E3,0",
            )
        )
        with mock.patch("subprocess.run") as run:
            bridge._play_wav(Path("/tmp/feedback.wav"))
        run.assert_called_once_with(
            [
                "paplay",
                "--device",
                "alsa_output.usb-E3",
                "/tmp/feedback.wav",
            ],
            check=True,
        )

    def test_unconfirmed_recording_rollback_retries_until_stopped(self):
        bridge = OpenClawVoiceBridge(SimpleNamespace())
        with (
            mock.patch.object(
                bridge,
                "execute_local_command",
                side_effect=[RuntimeError("dashboard restarting"), "recording_stopped"],
            ) as execute,
            mock.patch("time.sleep") as sleep,
        ):
            bridge._rollback_unconfirmed_recording()
        self.assertEqual(execute.call_count, 2)
        execute.assert_called_with("recording_stop")
        sleep.assert_called_once_with(0.5)

    def test_wake_followup_forces_openclaw_even_for_fixed_command(self):
        bridge = OpenClawVoiceBridge(SimpleNamespace())
        with (
            mock.patch.object(bridge, "execute_local_command") as execute,
            mock.patch.object(bridge, "ask_openclaw", return_value="好的。") as ask,
            mock.patch.object(bridge, "speak") as speak,
            mock.patch.object(
                bridge, "_read_recording_status", return_value={"recording": False}
            ),
        ):
            bridge.handle_utterance("开始录制", allow_local_commands=False)
        execute.assert_not_called()
        ask.assert_called_once_with("开始录制")
        speak.assert_called_once_with("好的。")

    def test_openclaw_recording_is_rolled_back_when_reply_is_not_played(self):
        bridge = OpenClawVoiceBridge(SimpleNamespace())
        with (
            mock.patch.object(bridge, "ask_openclaw", return_value="录制已经开始。"),
            mock.patch.object(bridge, "speak", return_value=False),
            mock.patch.object(
                bridge,
                "_read_recording_status",
                side_effect=[
                    {"recording": False},
                    {
                        "recording": True,
                        "output_path": "/bags/looper_record_20260814_160000",
                    },
                ],
            ),
            mock.patch.object(bridge, "_rollback_unconfirmed_recording") as rollback,
        ):
            bridge.handle_utterance("帮我开始这一轮采集", allow_local_commands=False)
        rollback.assert_called_once_with()

    def test_unplayed_openclaw_reply_does_not_stop_manual_recording(self):
        bridge = OpenClawVoiceBridge(SimpleNamespace())
        with (
            mock.patch.object(bridge, "ask_openclaw", return_value="好的。"),
            mock.patch.object(bridge, "speak", return_value=False),
            mock.patch.object(
                bridge,
                "_read_recording_status",
                side_effect=[
                    {"recording": False},
                    {"recording": True, "output_path": "/bags/manual_capture"},
                ],
            ),
            mock.patch.object(bridge, "_rollback_unconfirmed_recording") as rollback,
        ):
            bridge.handle_utterance("查看录制状态", allow_local_commands=False)
        rollback.assert_not_called()

    def test_local_command_calls_dashboard_automation_endpoint(self):
        bridge = OpenClawVoiceBridge(
            SimpleNamespace(
                dashboard_url="http://127.0.0.1:8765/",
                dashboard_timeout_sec=7.0,
            )
        )
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = (
            b'{"recording": true, "start_timings": '
            b'{"resume_requested_offset_sec": 2.5, "total_sec": 2.7}}'
        )
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
        self.assertGreaterEqual(
            bridge._last_local_command_timing["dashboard_request_sec"], 0.0
        )
        self.assertEqual(
            bridge._last_local_command_timing["dashboard_start_timings"]["total_sec"],
            2.7,
        )

    def test_local_recording_emits_end_to_end_timing(self):
        bridge = OpenClawVoiceBridge(SimpleNamespace())
        bridge._last_recognition_timing = {
            "vad_silence_sec": 0.5,
            "decode_sec": 0.08,
        }
        bridge._last_local_command_timing = {}

        def execute(_action):
            bridge._last_local_command_timing = {
                "dashboard_request_sec": 2.8,
                "dashboard_start_timings": {
                    "resume_requested_offset_sec": 2.4,
                    "resume_confirmed_offset_sec": 2.6,
                    "total_sec": 2.7,
                },
            }
            return "recording_started"

        with (
            mock.patch.object(bridge, "speak_canned", return_value=True),
            mock.patch.object(bridge, "execute_local_command", side_effect=execute),
            mock.patch.object(bridge, "_emit") as emit,
        ):
            bridge.handle_utterance("开始录制")

        timing_event = next(
            call for call in emit.call_args_list
            if call.args == ("local_command_timing",)
        )
        self.assertEqual(timing_event.kwargs["action"], "recording_start")
        self.assertEqual(timing_event.kwargs["recognition"]["decode_sec"], 0.08)
        self.assertGreaterEqual(
            timing_event.kwargs["recognition_to_resume_confirmed_sec"], 2.6
        )

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

    def test_capture_check_command_calls_local_quality_gate(self):
        bridge = OpenClawVoiceBridge(
            SimpleNamespace(
                dashboard_url="http://127.0.0.1:8765",
                dashboard_timeout_sec=7.0,
            )
        )
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"state": "pass"}'
        with mock.patch("urllib.request.urlopen", return_value=response) as urlopen:
            reply_key = bridge.execute_local_command("capture_check")
        self.assertEqual(reply_key, "capture_check_pass")
        self.assertEqual(
            urlopen.call_args.args[0].full_url,
            "http://127.0.0.1:8765/api/capture-check/run",
        )

    def test_capture_check_announces_start_before_calling_dashboard(self):
        bridge = OpenClawVoiceBridge(SimpleNamespace())
        events = []

        def speak(key):
            events.append(("speak", key))
            return True

        def execute(action):
            events.append(("execute", action))
            return "capture_check_pass"

        with (
            mock.patch.object(bridge, "speak_canned", side_effect=speak),
            mock.patch.object(bridge, "execute_local_command", side_effect=execute),
        ):
            bridge.handle_utterance("检测相机")
        self.assertEqual(
            events,
            [
                ("speak", "capture_check_started"),
                ("execute", "capture_check"),
                ("speak", "capture_check_pass"),
            ],
        )

    def test_capture_check_is_not_run_without_start_feedback(self):
        bridge = OpenClawVoiceBridge(SimpleNamespace())
        with (
            mock.patch.object(bridge, "speak_canned", return_value=False) as speak,
            mock.patch.object(bridge, "execute_local_command") as execute,
        ):
            bridge.handle_utterance("检查相机")
        speak.assert_called_once_with("capture_check_started")
        execute.assert_not_called()

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

    def test_capture_check_states_have_deterministic_canned_replies(self):
        self.assertEqual(capture_check_reply_key({"state": "pass"}), "capture_check_pass")
        self.assertEqual(capture_check_reply_key({"state": "retry"}), "capture_check_retry")
        self.assertEqual(
            capture_check_reply_key({"state": "recalibrate"}),
            "capture_check_recalibrate",
        )
        self.assertEqual(
            capture_check_reply_key({"state": "reference_saved"}, reference=True),
            "capture_reference_saved",
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
        self.assertEqual(args.playback_backend, "pulse")
        self.assertEqual(args.agent_thinking, "off")
        self.assertEqual(args.dashboard_timeout_sec, 40.0)


if __name__ == "__main__":
    unittest.main()
