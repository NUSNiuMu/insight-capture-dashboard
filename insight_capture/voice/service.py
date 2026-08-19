#!/usr/bin/env python3
"""Run a local wake-word/STT loop and send utterances to OpenClaw."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
import re
import struct
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path
from typing import Iterable, Optional


DEFAULT_DATA_ROOT = Path.home() / ".local" / "share" / "looper-voice"
DEFAULT_SENSE_VOICE_ROOT = (
    DEFAULT_DATA_ROOT
    / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
)
DEFAULT_WAKE_PHRASE = "宸境"
DEFAULT_WAKE_ALIASES = (
    "澄净",
    "澄静",
    "成静",
    "沉静",
    "沉浸",
    "很静",
    "南静",
    "辰境",
    "晨景",
    "晨静",
    "陈静",
)

LOCAL_COMMAND_ALIASES = {
    "recording_start": ("开始录制", "开始录像", "开始采集"),
    "recording_stop": (
        "结束录制",
        "停止录制",
        "结束录像",
        "停止录像",
        "结束采集",
        "停止采集",
    ),
    "calibration_start": ("开始校准", "重新校准", "重置校准"),
    "capture_check": ("检查相机", "开始检测", "位置检测", "检测相机"),
    "capture_reference": ("设置检测位", "记录检测位", "保存检测位"),
    "system_status": ("系统状态", "检查系统", "数采状态"),
    "take_reject": ("本条作废", "这条作废", "作废本条"),
}
LOCAL_COMMAND_ENDPOINTS = {
    "recording_start": "/api/automation/recording/start",
    "recording_stop": "/api/automation/recording/stop",
    "calibration_start": "/api/mapping/reset",
    "capture_check": "/api/capture-check/run",
    "capture_reference": "/api/capture-check/reference",
    "system_status": "/api/system/status",
    "take_reject": "/api/takes/current/reject",
}
LOCAL_COMMAND_REPLY_KEYS = {
    "recording_start": "recording_started",
    "recording_stop": "recording_stopped",
    "calibration_start": "calibration_started",
    "capture_check": "capture_check_not_ready",
    "capture_reference": "capture_reference_saved",
    "system_status": "dynamic_reply",
    "take_reject": "dynamic_reply",
}


class LocalCommandFailure(RuntimeError):
    """A deterministic command failure with operator-facing speech."""

    def __init__(self, message: str, speech: Optional[str] = None) -> None:
        super().__init__(message)
        self.speech = str(speech or "").strip()


VOLUME_SAMPLE_TEXT = "宸境科技"
CANNED_REPLIES = {
    "recording_starting": "初始化录制中，请稍等。",
    "recording_started": "录制已经开始。",
    "recording_stopping": "正在结束录制。",
    "recording_stopped": "录制已经结束。请将左右手相机放回检测位，并用头部相机扫视已建图工作区，然后说检查相机。",
    "calibration_started": "校准已经开始。",
    "calibration_completed": "校准完成。",
    "capture_check_started": "开始检测。",
    "capture_reference_saved": "双手检测位和头部相机地图基准已经记录。",
    "capture_check_pass": "双手相机位置和头部相机地图闭环正常，可以开始下一次采集。",
    "capture_check_retry": "检测尚未通过。请确认左右手相机完全归位，并用头部相机扫视已建图工作区，然后再说检查相机。",
    "capture_check_recalibrate": "相机位置或头部地图闭环异常。请说开始校准，完成后重新设置检测位。",
    "capture_check_not_ready": "检测条件未满足。请确认左右手相机已经静止，头部相机和两路全局定位服务在线。",
    "capture_check_no_reference": "还没有检测位基准。请放好左右手相机，确认建图完成，然后说设置检测位。",
    "recording_already_active": "当前已经在录制。",
    "command_failed": "指令执行失败，请检查数采服务。",
}


def normalize_transcript(text: object) -> str:
    """Normalize a wake transcript without accepting substring matches."""
    return " ".join(re.findall(r"[0-9a-zA-Z]+|[\u4e00-\u9fff]+", str(text or "").lower()))


def wake_word_detected(text: object, wake_phrases: Iterable[str]) -> bool:
    transcript = normalize_transcript(text)
    if not transcript:
        return False
    compact = transcript.replace(" ", "")
    for phrase in wake_phrases:
        normalized = normalize_transcript(phrase)
        if normalized and (
            normalized in transcript.split() or normalized.replace(" ", "") == compact
        ):
            return True
    return False


def match_local_command(text: object) -> Optional[str]:
    """Return an exact local action for a short deterministic command."""
    normalized = normalize_transcript(text).replace(" ", "")
    for prefix in ("请帮我", "帮我", "请"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    for suffix in ("一下", "吧"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    for action, aliases in LOCAL_COMMAND_ALIASES.items():
        if normalized in aliases:
            return action
    return None


def calibration_is_complete(payload: object) -> bool:
    """Return whether both Insight3 devices have a first global localization."""
    if not isinstance(payload, dict):
        return False
    statuses = payload.get("statuses")
    if not isinstance(statuses, dict):
        return False
    return all(
        isinstance(statuses.get(name), dict)
        and bool(statuses[name].get("localized"))
        for name in ("insight3_a", "insight3_b")
    )


def capture_check_reply_key(payload: object, *, reference: bool = False) -> str:
    """Map deterministic station-check states to pre-generated speech."""
    if not isinstance(payload, dict):
        return "capture_check_not_ready"
    state = str(payload.get("state") or "not_ready")
    if reference and state == "reference_saved":
        return "capture_reference_saved"
    return {
        "pass": "capture_check_pass",
        "retry": "capture_check_retry",
        "recalibrate": "capture_check_recalibrate",
        "no_reference": "capture_check_no_reference",
    }.get(state, "capture_check_not_ready")


def extract_openclaw_reply(payload: object) -> str:
    if not isinstance(payload, dict):
        raise ValueError("OpenClaw returned a non-object JSON payload")
    result = payload.get("result")
    if isinstance(result, dict):
        payload = result
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str) and content.strip():
            return content.strip()
    meta = payload.get("meta")
    if isinstance(meta, dict):
        visible = meta.get("finalAssistantVisibleText")
        if isinstance(visible, str) and visible.strip():
            return visible.strip()
    texts = []
    for item in payload.get("payloads", []):
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            texts.append(item["text"].strip())
    reply = "\n".join(text for text in texts if text).strip()
    if not reply:
        raise ValueError("OpenClaw returned no assistant text")
    return reply


def speech_text(text: object, max_chars: int = 240) -> str:
    """Remove formatting that sounds unnatural when spoken."""
    value = str(text or "")
    value = re.sub(r"\[([^\]]+)]\([^)]+\)", r"\1", value)
    value = re.sub(r"\[\[tts:[^\]]+]]", "", value)
    value = value.replace("[[/tts:text]]", "").replace("[[tts:text]]", "")
    value = re.sub(r"[`*_#>|]", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= max_chars:
        return value
    return value[: max(1, max_chars - 1)].rstrip("，。！？；：,.!?;:") + "。"


def clean_utterance_transcript(text: object) -> str:
    """Join CJK tokens and repair narrow, domain-specific homophones."""
    value = str(text or "").strip()
    value = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", value)
    for homophone in ("素材", "素菜"):
        value = value.replace(f"{homophone}状态", "数采状态")
        value = value.replace(f"{homophone}系统", "数采系统")
    return re.sub(r"\s+", " ", value).strip()


def strip_wake_prefix(text: object, wake_phrases: Iterable[str]) -> str:
    """Remove a leading wake phrase without touching the rest of the command."""
    value = str(text or "").strip()
    for phrase in sorted(wake_phrases, key=len, reverse=True):
        value = re.sub(
            rf"^\s*{re.escape(phrase)}(?:\s|[，。！？,.!?:：；;-])*",
            "",
            value,
            count=1,
            flags=re.IGNORECASE,
        )
    return value.strip()


def wake_tone_wav(
    sample_rate: int,
    duration_ms: int,
    frequency_hz: int,
    volume: float,
) -> bytes:
    """Build a short in-memory WAV used as immediate wake feedback."""
    frame_count = max(1, round(sample_rate * duration_ms / 1000))
    fade_frames = max(1, min(frame_count // 2, round(sample_rate * 0.02)))
    pcm = bytearray()
    for index in range(frame_count):
        envelope = min(1.0, index / fade_frames, (frame_count - index) / fade_frames)
        sample = int(
            32767
            * volume
            * envelope
            * math.sin(2.0 * math.pi * frequency_hz * index / sample_rate)
        )
        pcm.extend(struct.pack("<h", sample))
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return output.getvalue()


def build_agent_command(
    openclaw_bin: Path,
    session_key: str,
    utterance: str,
    timeout_sec: int,
    thinking_level: str = "off",
    model: str = "openai/gpt-5.6-luna",
) -> list[str]:
    prompt = (
        "以下内容由本地麦克风离线转写。按宸境语音助手规则处理；"
        "除非用户明确要求详情，否则最多用两句简短中文回答。\n\n"
        f"用户说：{utterance}"
    )
    return [
        str(openclaw_bin),
        "agent",
        "--session-key",
        session_key,
        "--model",
        model,
        "--message",
        prompt,
        "--thinking",
        thinking_level,
        "--timeout",
        str(timeout_sec),
        "--json",
    ]


def discover_alsa_device(kind: str, preferred: str = "E3") -> str:
    """Resolve a stable ALSA CARD identifier, falling back to the default."""
    binary = "arecord" if kind == "capture" else "aplay"
    try:
        completed = subprocess.run(
            [binary, "-L"], check=False, capture_output=True, text=True, timeout=3.0
        )
    except (OSError, subprocess.SubprocessError):
        return "default"
    candidates = [line.strip() for line in completed.stdout.splitlines() if line and not line[0].isspace()]
    stable = [name for name in candidates if name.startswith("plughw:CARD=")]
    hint = str(preferred or "").casefold()
    if hint:
        for name in stable:
            if hint in name.casefold():
                return name
    return stable[0] if stable else "default"


class AlsaCapture:
    def __init__(self, device: str, sample_rate: int, chunk_frames: int) -> None:
        self.device = device
        self.sample_rate = sample_rate
        self.chunk_frames = chunk_frames
        self.process: Optional[subprocess.Popen] = None

    def __enter__(self) -> "AlsaCapture":
        self.process = subprocess.Popen(
            [
                "arecord",
                "-q",
                "-D",
                self.device,
                "-f",
                "S16_LE",
                "-r",
                str(self.sample_rate),
                "-c",
                "1",
                "-t",
                "raw",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if self.process.stdout is None:
            raise RuntimeError("ALSA capture stdout is unavailable")
        return self

    def read(self) -> bytes:
        assert self.process is not None and self.process.stdout is not None
        audio = self.process.stdout.read(self.chunk_frames * 2)
        if not audio:
            raise RuntimeError(f"ALSA capture stopped (exit {self.process.poll()})")
        return audio

    def __exit__(self, *_exc_info) -> None:
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2.0)


# Keep the service focused on orchestration. These imports intentionally rebind
# the legacy module-level names so existing callers keep a stable facade.
from insight_capture.voice.audio import (
    AlsaCapture,
    configure_pulse_card_volume,
    discover_alsa_device,
    discover_pulse_sink,
    list_alsa_devices,
    set_alsa_playback_volume,
    set_pulse_sink_volume,
    wake_tone_wav,
)
from insight_capture.voice.commands import (
    LOCAL_COMMAND_ALIASES,
    LOCAL_COMMAND_ENDPOINTS,
    LOCAL_COMMAND_REPLY_KEYS,
    LocalCommandFailure,
    calibration_is_complete,
    capture_check_reply_key,
    capture_check_speech,
    match_local_command,
    normalize_transcript,
    wake_word_detected,
)
from insight_capture.voice.control import (
    VoiceControlServer,
    load_playback_volume,
    save_playback_volume,
    validate_playback_volume,
)
from insight_capture.voice.openclaw_adapter import build_agent_command, extract_openclaw_reply
from insight_capture.voice.replies import (
    CANNED_REPLIES,
    clean_utterance_transcript,
    speech_text,
    strip_wake_prefix,
)
from insight_capture.voice.stt import decode as decode_sense_voice
from insight_capture.voice.tts import synthesize_espeak, synthesize_piper_wav


class VoiceService:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self._sense_voice = None
        self._sherpa_onnx = None
        self._piper_voice = None
        self._piper_synthesis_config = None
        self._wake_feedback_audio: Optional[Path] = None
        self._canned_audio: dict[str, Path] = {}
        self._playback_lock = threading.Lock()
        self._control_server: Optional[VoiceControlServer] = None
        self._calibration_monitor_generation = 0
        self._wake_candidates = 0
        self._last_recognition_timing: dict[str, object] = {}
        self._last_local_command_timing: dict[str, object] = {}
        self._pending_spoken_reply = ""
        self._alert_cursor: Optional[int] = None

    @staticmethod
    def _emit(event: str, **payload: object) -> None:
        print(json.dumps({"event": event, **payload}, ensure_ascii=False), flush=True)

    def load_models(self) -> None:
        if not self.args.vad_model.is_file():
            raise FileNotFoundError(f"VAD model not found: {self.args.vad_model}")
        import sherpa_onnx

        self._sherpa_onnx = sherpa_onnx
        for asset in (self.args.sense_voice_model, self.args.sense_voice_tokens):
            if not asset.is_file():
                raise FileNotFoundError(f"SenseVoice asset not found: {asset}")
        self._sense_voice = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(self.args.sense_voice_model),
            tokens=str(self.args.sense_voice_tokens),
            num_threads=self.args.speech_threads,
            language="zh",
            use_itn=True,
            debug=False,
        )
        self._prepare_wake_feedback()
        self._prepare_canned_audio()

    def _load_piper(self) -> None:
        if self._piper_voice is not None:
            return
        if not self.args.piper_model.is_file():
            raise FileNotFoundError(f"Piper voice model not found: {self.args.piper_model}")
        from piper import PiperVoice, SynthesisConfig

        self._piper_voice = PiperVoice.load(self.args.piper_model)
        self._piper_synthesis_config = SynthesisConfig(
            length_scale=self.args.piper_length_scale,
            noise_scale=self.args.piper_noise_scale,
            noise_w_scale=self.args.piper_noise_w_scale,
        )

    def _synthesize_piper(self, text: str, output_path: Path) -> None:
        self._load_piper()
        synthesize_piper_wav(
            self._piper_voice,
            text,
            output_path,
            self._piper_synthesis_config,
            sentence_silence_ms=self.args.piper_sentence_silence_ms,
        )

    def _prepare_wake_feedback(self) -> None:
        if self.args.no_tts or self.args.wake_feedback != "speech":
            return
        output_path = Path(tempfile.gettempdir()) / f"looper-wake-{os.getuid()}.wav"
        self._synthesize_piper(self.args.acknowledgement, output_path)
        self._wake_feedback_audio = output_path

    def _synthesize_text(self, text: str, output_path: Path) -> None:
        if self.args.tts_engine == "piper":
            self._synthesize_piper(text, output_path)
            return
        synthesize_espeak(
            self.args.tts_bin,
            self.args.tts_voice,
            self.args.tts_speed,
            text,
            output_path,
        )

    def _prepare_canned_audio(self) -> None:
        if self.args.no_tts:
            return
        output_dir = Path(tempfile.gettempdir()) / f"looper-canned-{os.getuid()}"
        output_dir.mkdir(parents=True, exist_ok=True)
        for key, text in CANNED_REPLIES.items():
            output_path = output_dir / f"{key}.wav"
            self._synthesize_text(text, output_path)
            self._canned_audio[key] = output_path

    def _capture(self) -> AlsaCapture:
        return AlsaCapture(
            self.args.device,
            self.args.sample_rate,
            self.args.chunk_frames,
        )

    def wait_for_activation(
        self,
        capture: Optional[AlsaCapture] = None,
        *,
        allow_local_commands: bool = True,
    ) -> tuple[str, str]:
        import numpy as np

        vad, window_size = self._new_vad(
            min_silence_sec=self.args.wake_pause_sec,
            min_speech_sec=0.1,
            max_speech_sec=5.0,
        )
        pending = np.empty(0, dtype=np.float32)
        wake_variants = [*self.args.wake_phrase, *self.args.wake_alias]
        self._emit(
            "listening",
            mode="activation" if allow_local_commands else "wake",
            engine="sensevoice",
            phrases=self.args.wake_phrase,
            direct_commands=allow_local_commands,
            pause_sec=self.args.wake_pause_sec,
        )
        capture_context = contextlib.nullcontext(capture) if capture else self._capture()
        with capture_context as active_capture:
            while True:
                audio = active_capture.read()
                samples = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
                pending = np.concatenate((pending, samples))
                while len(pending) >= window_size:
                    vad.accept_waveform(pending[:window_size])
                    pending = pending[window_size:]
                    if not vad.empty():
                        segment = np.copy(vad.front.samples)
                        vad.pop()
                        self._wake_candidates += 1
                        transcript = self._decode_sense_voice(
                            segment,
                            mode="activation" if allow_local_commands else "wake",
                        )
                        if self.args.log_wake_candidates:
                            self._emit(
                                "wake_candidate",
                                recognized_text=transcript,
                                candidate=self._wake_candidates,
                            )
                        local_action = (
                            match_local_command(transcript)
                            if allow_local_commands
                            else None
                        )
                        if local_action is not None:
                            self._last_recognition_timing.update(
                                {
                                    "vad_silence_sec": round(
                                        self.args.wake_pause_sec, 4
                                    ),
                                    "capture_chunk_sec": round(
                                        self.args.chunk_frames / self.args.sample_rate, 4
                                    ),
                                    "transcript": transcript,
                                }
                            )
                            self._emit(
                                "direct_command",
                                text=transcript,
                                action=local_action,
                                engine="sensevoice",
                            )
                            return "local_command", transcript
                        if wake_word_detected(transcript, wake_variants):
                            self._emit(
                                "wake",
                                text=self.args.wake_phrase[0],
                                recognized_text=transcript,
                                engine="sensevoice",
                                pause_sec=self.args.wake_pause_sec,
                                candidates=self._wake_candidates,
                            )
                            return "wake", transcript

    def wait_for_wake_word(self, capture: Optional[AlsaCapture] = None) -> None:
        self.wait_for_activation(capture, allow_local_commands=False)

    def _new_vad(
        self,
        *,
        min_silence_sec: Optional[float] = None,
        min_speech_sec: Optional[float] = None,
        max_speech_sec: Optional[float] = None,
    ):
        config = self._sherpa_onnx.VadModelConfig()
        config.silero_vad.model = str(self.args.vad_model)
        config.silero_vad.threshold = self.args.vad_threshold
        config.silero_vad.min_silence_duration = (
            self.args.vad_silence_sec if min_silence_sec is None else min_silence_sec
        )
        config.silero_vad.min_speech_duration = (
            self.args.vad_min_speech_sec if min_speech_sec is None else min_speech_sec
        )
        config.silero_vad.max_speech_duration = (
            self.args.vad_max_speech_sec if max_speech_sec is None else max_speech_sec
        )
        config.sample_rate = self.args.sample_rate
        vad = self._sherpa_onnx.VoiceActivityDetector(
            config,
            buffer_size_in_seconds=max(
                30,
                int(config.silero_vad.max_speech_duration + 5),
            ),
        )
        return vad, config.silero_vad.window_size

    def _decode_sense_voice(self, samples, *, mode: str = "command") -> str:
        started = time.monotonic()
        decoded = decode_sense_voice(self._sense_voice, self.args.sample_rate, samples)
        decode_sec = time.monotonic() - started
        text = clean_utterance_transcript(decoded)
        self._last_recognition_timing = {
            "mode": mode,
            "audio_sec": round(len(samples) / self.args.sample_rate, 4),
            "decode_sec": round(decode_sec, 4),
        }
        self._emit(
            "recognition",
            engine="sensevoice",
            mode=mode,
            audio_sec=round(len(samples) / self.args.sample_rate, 4),
            decode_sec=round(decode_sec, 4),
        )
        return text

    def _listen_with_sense_voice(
        self,
        timeout_sec: float,
        capture: Optional[AlsaCapture] = None,
        initial_audio: bytes = b"",
    ) -> Optional[str]:
        import numpy as np

        vad, window_size = self._new_vad()
        deadline = time.monotonic() + timeout_sec
        pending = np.empty(0, dtype=np.float32)
        captured = []
        heard_speech = False
        self._emit(
            "listening",
            mode="command",
            engine="sensevoice",
            timeout_sec=timeout_sec,
        )
        capture_context = contextlib.nullcontext(capture) if capture else self._capture()
        queued_audio = initial_audio
        with capture_context as active_capture:
            while time.monotonic() < deadline:
                raw = queued_audio or active_capture.read()
                queued_audio = b""
                samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                captured.append(samples)
                pending = np.concatenate((pending, samples))
                while len(pending) >= window_size:
                    vad.accept_waveform(pending[:window_size])
                    pending = pending[window_size:]
                    heard_speech = heard_speech or vad.is_speech_detected()
                    if not vad.empty():
                        segment = np.copy(vad.front.samples)
                        vad.pop()
                        text = self._decode_sense_voice(segment)
                        text = strip_wake_prefix(
                            text, [*self.args.wake_phrase, *self.args.wake_alias]
                        )
                        if text:
                            self._emit("transcript", text=text)
                            return text
            if heard_speech and captured:
                text = self._decode_sense_voice(np.concatenate(captured))
                text = strip_wake_prefix(
                    text, [*self.args.wake_phrase, *self.args.wake_alias]
                )
                if text:
                    self._emit("transcript", text=text)
                    return text
        return None

    def listen_for_utterance(
        self,
        timeout_sec: float,
        capture: Optional[AlsaCapture] = None,
        initial_audio: bytes = b"",
    ) -> Optional[str]:
        return self._listen_with_sense_voice(timeout_sec, capture, initial_audio)

    def play_wake_feedback(self) -> None:
        if self.args.wake_feedback == "none":
            return
        if self.args.wake_feedback == "speech":
            if self.args.no_tts:
                self._emit("wake_feedback", kind="speech", played=False)
                return
            if self._wake_feedback_audio is None:
                raise RuntimeError("Wake acknowledgement audio was not prepared")
            with self._playback_lock:
                self._play_wav(self._wake_feedback_audio)
            self._emit("wake_feedback", kind="speech", played=True)
            return
        with tempfile.NamedTemporaryFile(prefix="looper-wake-", suffix=".wav") as audio:
            audio.write(
                wake_tone_wav(
                    self.args.sample_rate,
                    self.args.wake_tone_ms,
                    self.args.wake_tone_frequency,
                    self.args.wake_tone_volume,
                )
            )
            audio.flush()
            with self._playback_lock:
                self._play_wav(Path(audio.name))
        self._emit("wake_feedback", kind="tone", played=True)

    def _play_wav(self, audio_path: Path) -> None:
        """Play a WAV through the configured shared or direct audio backend."""
        if self.args.playback_backend == "pulse":
            command = ["paplay"]
            if self.args.pulse_sink:
                command.extend(["--device", self.args.pulse_sink])
            command.append(str(audio_path))
        else:
            command = [
                "aplay",
                "-q",
                "-D",
                self.args.playback_device,
                str(audio_path),
            ]
        try:
            subprocess.run(command, check=True)
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(
                f"Audio feedback failed via {self.args.playback_backend}: {exc}"
            ) from exc

    def _refresh_playback_device(self) -> dict[str, str]:
        options = list_alsa_devices("playback")
        if not options:
            raise RuntimeError("no playback device is currently available")
        selected = next(
            (
                option
                for option in options
                if option["id"] == self.args.playback_device
            ),
            options[0],
        )
        device_changed = selected["id"] != self.args.playback_device
        self.args.playback_device = selected["id"]
        sink_changed = False
        if self.args.playback_backend == "pulse":
            pulse_sink = discover_pulse_sink("", self.args.playback_device)
            if pulse_sink and pulse_sink != self.args.pulse_sink:
                self.args.pulse_sink = pulse_sink
                sink_changed = True
        if device_changed or sink_changed:
            self._emit(
                "playback_device",
                device=self.args.playback_device,
                pulse_sink=self.args.pulse_sink or "default",
                source="hotplug",
            )
        return selected

    def audio_control_status(self) -> dict[str, object]:
        selected = self._refresh_playback_device()
        return {
            "available": True,
            "volume_percent": self.args.playback_volume,
            "backend": self.args.playback_backend,
            "playback_device": self.args.playback_device,
            "playback_label": selected["label"],
            "pulse_sink": self.args.pulse_sink or "default",
        }

    def set_playback_volume(self, value: object) -> dict[str, object]:
        volume = validate_playback_volume(value)
        with self._playback_lock:
            self._refresh_playback_device()
            if self.args.playback_backend == "pulse":
                applied = set_pulse_sink_volume(self.args.pulse_sink, volume)
            else:
                applied = set_alsa_playback_volume(
                    self.args.playback_device,
                    volume,
                )
            if not applied:
                raise RuntimeError("audio device rejected the volume change")
            save_playback_volume(self.args.audio_settings, volume)
            self.args.playback_volume = volume
        self._emit("playback_volume", volume_percent=volume, source="web")
        return self.audio_control_status()

    def play_volume_sample(self) -> dict[str, object]:
        self.speak(VOLUME_SAMPLE_TEXT)
        return self.audio_control_status()

    def _start_control_server(self) -> None:
        self._control_server = VoiceControlServer(
            self.args.control_host,
            self.args.control_port,
            self.audio_control_status,
            self.set_playback_volume,
            self.play_volume_sample,
        )
        self._control_server.start()
        self._emit(
            "control_ready",
            host=self.args.control_host,
            port=self.args.control_port,
        )

    def ask_openclaw(self, utterance: str) -> str:
        if not self.args.openclaw_bin.is_file():
            raise RuntimeError("OpenClaw optional assistant is not installed")
        command = build_agent_command(
            self.args.openclaw_bin,
            self.args.session_key,
            utterance,
            self.args.agent_timeout_sec,
            self.args.agent_thinking,
            self.args.agent_model,
        )
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=self.args.agent_timeout_sec + 15,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip().splitlines()
            message = detail[-1] if detail else f"exit {completed.returncode}"
            raise RuntimeError(f"OpenClaw agent failed: {message}")
        return speech_text(extract_openclaw_reply(json.loads(completed.stdout)))

    def execute_local_command(self, action: str) -> str:
        endpoint = LOCAL_COMMAND_ENDPOINTS[action]
        request = urllib.request.Request(
            f"{self.args.dashboard_url.rstrip('/')}{endpoint}",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        request_started = time.monotonic()
        try:
            with urllib.request.urlopen(
                request, timeout=self.args.dashboard_timeout_sec
            ) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if action == "recording_start" and exc.code == 409:
                return "recording_already_active"
            try:
                error_payload = json.loads(exc.read())
            except (ValueError, OSError):
                error_payload = {}
            speech = error_payload.get("speech") if isinstance(error_payload, dict) else None
            raise LocalCommandFailure(
                f"Dashboard API returned HTTP {exc.code}", speech
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Dashboard API returned a non-object payload")
        request_sec = time.monotonic() - request_started
        if action == "recording_start" and not payload.get("recording"):
            raise RuntimeError("Dashboard did not start recording")
        if action == "recording_stop" and payload.get("recording"):
            raise RuntimeError("Dashboard did not stop recording")
        if action == "calibration_start" and not payload.get("ok"):
            message = payload.get("error") or "Dashboard did not reset calibration"
            raise RuntimeError(str(message))
        dashboard_start_timings = (
            dict(payload.get("start_timings") or {})
            if action == "recording_start"
            else {}
        )
        self._last_local_command_timing = {
            "dashboard_request_sec": round(request_sec, 4),
            "dashboard_start_timings": dashboard_start_timings,
        }
        self._emit(
            "local_command",
            action=action,
            endpoint=endpoint,
            ok=True,
            **self._last_local_command_timing,
        )
        if action == "calibration_start":
            self._start_calibration_monitor()
        if action == "capture_check":
            detail = capture_check_speech(payload)
            if detail:
                self._pending_spoken_reply = detail
                return "dynamic_reply"
            return capture_check_reply_key(payload)
        if action == "capture_reference":
            return capture_check_reply_key(payload, reference=True)
        if action in {
            "system_status",
            "take_reject",
            "task_cup_stacking_start",
            "task_status",
            "task_end",
        }:
            self._pending_spoken_reply = str(payload.get("speech") or "").strip()
            if not self._pending_spoken_reply:
                raise RuntimeError("Dashboard returned no spoken status")
            return "dynamic_reply"
        return LOCAL_COMMAND_REPLY_KEYS[action]

    def _start_alert_monitor(self) -> None:
        threading.Thread(
            target=self._monitor_voice_alerts,
            daemon=True,
            name="voice_active_qc_alerts",
        ).start()

    def _monitor_voice_alerts(self) -> None:
        while True:
            cursor = 0 if self._alert_cursor is None else self._alert_cursor
            request = urllib.request.Request(
                f"{self.args.dashboard_url.rstrip('/')}/api/voice/alerts?cursor={cursor}",
                method="GET",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.args.dashboard_timeout_sec) as response:
                    payload = json.loads(response.read())
                alerts = payload.get("alerts") if isinstance(payload, dict) else []
                latest = int(payload.get("cursor", cursor)) if isinstance(payload, dict) else cursor
                if self._alert_cursor is not None:
                    for alert in alerts or []:
                        text = str(alert.get("text") or "").strip()
                        if text:
                            self.speak(text)
                self._alert_cursor = latest
            except Exception as exc:  # noqa: BLE001 - fixed commands remain available
                self._emit("error", stage="active_qc_alerts", message=str(exc))
            time.sleep(1.0)

    def _recording_status(self) -> dict[str, object]:
        request = urllib.request.Request(
            f"{self.args.dashboard_url.rstrip('/')}/api/recording/status",
            method="GET",
        )
        with urllib.request.urlopen(
            request, timeout=self.args.dashboard_timeout_sec
        ) as response:
            payload = json.loads(response.read())
        if not isinstance(payload, dict):
            raise RuntimeError("Dashboard recording status was not an object")
        return payload

    def _read_recording_status(self, stage: str) -> Optional[dict[str, object]]:
        try:
            return self._recording_status()
        except Exception as exc:  # noqa: BLE001 - status is an added safety signal
            self._emit("error", stage=stage, message=str(exc))
            return None

    def _rollback_new_unconfirmed_recording(
        self, before: Optional[dict[str, object]]
    ) -> bool:
        if before is None or before.get("recording"):
            return False
        after = self._read_recording_status("recording_feedback_status_after")
        if after is None or not after.get("recording"):
            return False
        output_path = Path(str(after.get("output_path") or ""))
        take = after.get("current_take") if isinstance(after.get("current_take"), dict) else {}
        if not (
            output_path.name.startswith("looper_record_")
            or take.get("trigger") == "voice"
        ):
            return False
        self._emit(
            "recording_feedback_rollback",
            ok=False,
            reason="new_automation_recording_has_no_feedback",
            output_path=str(output_path),
        )
        self._rollback_unconfirmed_recording()
        return True

    def _start_calibration_monitor(self) -> None:
        self._calibration_monitor_generation += 1
        generation = self._calibration_monitor_generation
        thread = threading.Thread(
            target=self._monitor_calibration,
            args=(generation,),
            name="voice_calibration_monitor",
            daemon=True,
        )
        thread.start()

    def _monitor_calibration(self, generation: int) -> None:
        deadline = time.monotonic() + self.args.calibration_monitor_timeout_sec
        saw_incomplete = False
        self._emit("calibration_monitor", state="waiting", generation=generation)
        while (
            generation == self._calibration_monitor_generation
            and time.monotonic() < deadline
        ):
            request = urllib.request.Request(
                f"{self.args.dashboard_url.rstrip('/')}/api/mapping",
                method="GET",
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self.args.dashboard_timeout_sec
                ) as response:
                    payload = json.loads(response.read())
            except Exception as exc:  # noqa: BLE001 - retry transient local API errors
                self._emit(
                    "error",
                    stage="calibration_monitor",
                    message=str(exc),
                    generation=generation,
                )
                time.sleep(1.0)
                continue
            complete = calibration_is_complete(payload)
            if not complete:
                saw_incomplete = True
            elif saw_incomplete:
                self._emit(
                    "calibration_monitor",
                    state="completed",
                    generation=generation,
                )
                self.speak_canned("calibration_completed")
                return
            time.sleep(1.0)
        if generation == self._calibration_monitor_generation:
            self._emit(
                "calibration_monitor",
                state="timeout",
                generation=generation,
            )

    def speak_canned(self, key: str) -> bool:
        text = CANNED_REPLIES[key]
        if self.args.no_tts:
            self._emit("speech", text=text, canned=True, played=False)
            return False
        audio_path = self._canned_audio.get(key)
        if audio_path is None:
            return self.speak(text)
        with self._playback_lock:
            self._play_wav(audio_path)
        self._emit("speech", text=text, canned=True, played=True)
        return True

    def speak(self, text: str) -> bool:
        text = speech_text(text)
        if not text:
            return False
        if self.args.no_tts:
            self._emit("speech", text=text, played=False)
            return False
        with tempfile.NamedTemporaryFile(prefix="looper-reply-", suffix=".wav") as audio:
            self._synthesize_text(text, Path(audio.name))
            with self._playback_lock:
                self._play_wav(Path(audio.name))
        self._emit("speech", text=text, played=True)
        return True

    def _rollback_unconfirmed_recording(self) -> None:
        """Keep stopping until a recording with failed confirmation is no longer active."""
        attempt = 0
        while True:
            attempt += 1
            try:
                self.execute_local_command("recording_stop")
            except Exception as exc:  # noqa: BLE001 - recording safety takes priority
                self._emit(
                    "error",
                    stage="recording_feedback_rollback",
                    attempt=attempt,
                    message=str(exc),
                )
                time.sleep(min(5.0, 0.5 * attempt))
                continue
            self._emit(
                "recording_feedback_rollback",
                ok=True,
                attempt=attempt,
                reason="start_feedback_not_played",
            )
            return

    def handle_utterance(
        self, utterance: str, *, allow_local_commands: bool = True
    ) -> None:
        local_action = match_local_command(utterance) if allow_local_commands else None
        if local_action is not None:
            interaction_started = time.monotonic()
            self._last_local_command_timing = {}
            immediate_feedback_sec = 0.0
            immediate_feedback = {
                "recording_start": "recording_starting",
                "recording_stop": "recording_stopping",
                "capture_check": "capture_check_started",
            }.get(local_action)
            if immediate_feedback is not None:
                immediate_feedback_started = time.monotonic()
                try:
                    start_feedback_played = self.speak_canned(immediate_feedback)
                except Exception as exc:  # noqa: BLE001 - keep the listener alive
                    self._emit(
                        "error",
                        stage=f"{local_action}_start_feedback",
                        message=str(exc),
                    )
                    start_feedback_played = False
                immediate_feedback_sec = time.monotonic() - immediate_feedback_started
                if not start_feedback_played:
                    return
            command_succeeded = False
            command_started = time.monotonic()
            try:
                reply_key = self.execute_local_command(local_action)
                command_succeeded = True
            except LocalCommandFailure as exc:
                self._emit("error", stage="local_command", message=str(exc))
                reply_key = "dynamic_failure"
                self._pending_spoken_reply = exc.speech or "指令执行失败，请检查数采服务。"
            except Exception as exc:  # noqa: BLE001 - keep voice control available
                self._emit("error", stage="local_command", message=str(exc))
                reply_key = "command_failed"
            command_finished = time.monotonic()
            final_feedback_started = command_finished
            try:
                if reply_key in {"dynamic_reply", "dynamic_failure"}:
                    feedback_played = self.speak(self._pending_spoken_reply)
                else:
                    feedback_played = self.speak_canned(reply_key)
            except Exception as exc:  # noqa: BLE001 - do not crash after state change
                self._emit(
                    "error",
                    stage="local_command_feedback",
                    action=local_action,
                    message=str(exc),
                )
                feedback_played = False
            final_feedback_sec = time.monotonic() - final_feedback_started
            command_details = dict(self._last_local_command_timing)
            dashboard_timings = dict(
                command_details.get("dashboard_start_timings") or {}
            )
            timing = {
                "recognition": dict(self._last_recognition_timing),
                "immediate_feedback_sec": round(immediate_feedback_sec, 4),
                "command_dispatch_sec": round(command_finished - command_started, 4),
                "dashboard_request_sec": command_details.get("dashboard_request_sec"),
                "dashboard_start_timings": dashboard_timings,
                "final_feedback_sec": round(final_feedback_sec, 4),
                "post_recognition_total_sec": round(
                    time.monotonic() - interaction_started, 4
                ),
            }
            resume_requested = dashboard_timings.get("resume_requested_offset_sec")
            resume_confirmed = dashboard_timings.get("resume_confirmed_offset_sec")
            command_offset = command_started - interaction_started
            if isinstance(resume_requested, (int, float)):
                timing["recognition_to_resume_requested_sec"] = round(
                    command_offset + resume_requested, 4
                )
            if isinstance(resume_confirmed, (int, float)):
                timing["recognition_to_resume_confirmed_sec"] = round(
                    command_offset + resume_confirmed, 4
                )
            self._emit(
                "local_command_timing",
                action=local_action,
                ok=command_succeeded and feedback_played,
                **timing,
            )
            if (
                local_action == "recording_start"
                and command_succeeded
                and reply_key == "recording_started"
                and not feedback_played
            ):
                self._rollback_unconfirmed_recording()
            return
        recording_before = self._read_recording_status(
            "recording_feedback_status_before"
        )
        try:
            reply = self.ask_openclaw(utterance)
        except Exception as exc:  # noqa: BLE001 - stay available after transient failures
            self._emit("error", stage="agent", message=str(exc))
            self._rollback_new_unconfirmed_recording(recording_before)
            with contextlib.suppress(Exception):
                self.speak("OpenClaw 暂时不可用。")
            return
        self._emit("reply", text=reply)
        try:
            feedback_played = self.speak(reply)
        except Exception as exc:  # noqa: BLE001 - keep listening after playback errors
            self._emit("error", stage="agent_feedback", message=str(exc))
            feedback_played = False
        if not feedback_played:
            self._rollback_new_unconfirmed_recording(recording_before)

    def run_forever(self) -> None:
        try:
            self._start_control_server()
        except OSError as exc:
            self._emit("error", stage="control_server", message=str(exc))
        self.load_models()
        self._start_alert_monitor()
        self._emit(
            "ready",
            device=self.args.device,
            playback_backend=self.args.playback_backend,
            playback_device=self.args.playback_device,
            pulse_sink=self.args.pulse_sink or "default",
            wake_phrases=self.args.wake_phrase,
        )
        while True:
            activation, transcript = self.wait_for_activation()
            if activation == "local_command":
                self.handle_utterance(transcript, allow_local_commands=True)
                continue
            try:
                self.play_wake_feedback()
            except Exception as exc:  # noqa: BLE001 - keep the listener alive
                self._emit("error", stage="wake_feedback", message=str(exc))
                continue
            utterance = self.listen_for_utterance(self.args.command_timeout_sec)
            if not utterance:
                try:
                    self.speak("没听清。")
                except Exception as exc:  # noqa: BLE001 - keep the listener alive
                    self._emit("error", stage="no_speech_feedback", message=str(exc))
                continue
            self.handle_utterance(utterance, allow_local_commands=False)


# Historical public name retained for host scripts and downstream tests.
OpenClawVoiceBridge = VoiceService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default=os.environ.get("LOOPER_CAPTURE_DEVICE", "auto"))
    parser.add_argument("--playback-device", default=os.environ.get("LOOPER_PLAYBACK_DEVICE", "auto"))
    parser.add_argument(
        "--audio-device-hint",
        default=os.environ.get("LOOPER_AUDIO_DEVICE_HINT", ""),
        help="Optional ALSA CARD hint; auto scans for a duplex USB device by default.",
    )
    parser.add_argument(
        "--playback-backend",
        choices=("pulse", "alsa"),
        default=os.environ.get("LOOPER_PLAYBACK_BACKEND", "pulse"),
        help="Use shared PulseAudio playback by default; ALSA is an explicit fallback.",
    )
    parser.add_argument(
        "--pulse-sink",
        default=os.environ.get("LOOPER_PULSE_SINK", "auto"),
        help="PulseAudio sink name; auto prefers the stable audio-device hint.",
    )
    parser.add_argument(
        "--playback-volume",
        type=int,
        default=int(os.environ.get("LOOPER_PLAYBACK_VOLUME", "40")),
        help="Playback volume percentage restored whenever the service starts.",
    )
    parser.add_argument(
        "--audio-settings",
        type=Path,
        default=Path(
            os.environ.get(
                "LOOPER_VOICE_SETTINGS",
                str(DEFAULT_DATA_ROOT / "settings.json"),
            )
        ),
        help="Persistent host-side settings written by the Dashboard volume control.",
    )
    parser.add_argument(
        "--control-host",
        default=os.environ.get("LOOPER_CONTROL_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--control-port",
        type=int,
        default=int(os.environ.get("LOOPER_CONTROL_PORT", "8770")),
    )
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--chunk-frames", type=int, default=4000)
    parser.add_argument(
        "--sense-voice-model",
        type=Path,
        default=DEFAULT_SENSE_VOICE_ROOT / "model.int8.onnx",
    )
    parser.add_argument(
        "--sense-voice-tokens",
        type=Path,
        default=DEFAULT_SENSE_VOICE_ROOT / "tokens.txt",
    )
    parser.add_argument(
        "--vad-model",
        type=Path,
        default=DEFAULT_DATA_ROOT / "silero_vad.onnx",
    )
    parser.add_argument("--speech-threads", type=int, default=2)
    parser.add_argument("--vad-threshold", type=float, default=0.5)
    parser.add_argument("--vad-silence-sec", type=float, default=0.7)
    parser.add_argument("--vad-min-speech-sec", type=float, default=0.25)
    parser.add_argument("--vad-max-speech-sec", type=float, default=12.0)
    parser.add_argument("--wake-pause-sec", type=float, default=0.5)
    parser.add_argument("--log-wake-candidates", action="store_true")
    parser.add_argument("--wake-phrase", action="append", default=[])
    parser.add_argument("--wake-alias", action="append", default=[])
    parser.add_argument("--session-key", default="looper-voice")
    parser.add_argument("--agent-model", default="openai/gpt-5.6-luna")
    parser.add_argument(
        "--openclaw-bin",
        type=Path,
        default=Path.home() / ".openclaw" / "bin" / "openclaw",
    )
    parser.add_argument("--agent-timeout-sec", type=int, default=90)
    parser.add_argument(
        "--agent-thinking",
        choices=("off", "minimal", "low", "medium", "high", "xhigh", "adaptive", "max"),
        default="off",
    )
    parser.add_argument("--command-timeout-sec", type=float, default=15.0)
    parser.add_argument("--dashboard-url", default="http://127.0.0.1:8765")
    parser.add_argument("--dashboard-timeout-sec", type=float, default=40.0)
    parser.add_argument("--calibration-monitor-timeout-sec", type=float, default=600.0)
    parser.add_argument("--acknowledgement", default="我在。")
    parser.add_argument(
        "--wake-feedback",
        choices=("tone", "speech", "none"),
        default="speech",
    )
    parser.add_argument("--wake-tone-ms", type=int, default=140)
    parser.add_argument("--wake-tone-frequency", type=int, default=880)
    parser.add_argument("--wake-tone-volume", type=float, default=0.3)
    parser.add_argument("--tts-engine", choices=("piper", "espeak"), default="piper")
    parser.add_argument(
        "--piper-model",
        type=Path,
        default=DEFAULT_DATA_ROOT / "zh_CN-huayan-medium.onnx",
    )
    parser.add_argument("--piper-length-scale", type=float, default=1.0)
    parser.add_argument("--piper-noise-scale", type=float, default=0.45)
    parser.add_argument("--piper-noise-w-scale", type=float, default=0.5)
    parser.add_argument("--piper-sentence-silence-ms", type=int, default=220)
    parser.add_argument("--tts-bin", default="espeak-ng")
    parser.add_argument("--tts-voice", default="cmn")
    parser.add_argument("--tts-speed", type=int, default=185)
    parser.add_argument("--no-tts", action="store_true")
    test_mode = parser.add_mutually_exclusive_group()
    test_mode.add_argument(
        "--wake-only",
        action="store_true",
        help="Exit after detecting the wake phrase without sending anything to OpenClaw.",
    )
    test_mode.add_argument(
        "--transcribe-once",
        action="store_true",
        help="Transcribe one utterance and exit without sending anything to OpenClaw.",
    )
    test_mode.add_argument(
        "--speak-text",
        help="Synthesize and play one local test phrase, then exit.",
    )
    test_mode.add_argument(
        "--echo-once",
        action="store_true",
        help="Run one fully local wake, transcription, and spoken echo cycle.",
    )
    args = parser.parse_args()
    if not args.wake_phrase:
        args.wake_phrase = [DEFAULT_WAKE_PHRASE]
    if not args.wake_alias:
        args.wake_alias = list(DEFAULT_WAKE_ALIASES)
    args.sample_rate = max(8000, args.sample_rate)
    args.chunk_frames = max(400, args.chunk_frames)
    args.speech_threads = max(1, args.speech_threads)
    args.vad_threshold = max(0.1, min(0.9, args.vad_threshold))
    args.vad_silence_sec = max(0.1, args.vad_silence_sec)
    args.vad_min_speech_sec = max(0.1, args.vad_min_speech_sec)
    args.vad_max_speech_sec = max(1.0, args.vad_max_speech_sec)
    args.wake_pause_sec = max(0.2, args.wake_pause_sec)
    args.agent_timeout_sec = max(10, args.agent_timeout_sec)
    args.command_timeout_sec = max(1.0, args.command_timeout_sec)
    args.dashboard_timeout_sec = max(1.0, args.dashboard_timeout_sec)
    args.calibration_monitor_timeout_sec = max(
        5.0, args.calibration_monitor_timeout_sec
    )
    args.wake_tone_ms = max(50, min(1000, args.wake_tone_ms))
    args.wake_tone_frequency = max(100, min(4000, args.wake_tone_frequency))
    args.wake_tone_volume = max(0.05, min(1.0, args.wake_tone_volume))
    args.playback_volume = max(0, min(100, args.playback_volume))
    args.control_port = max(1, min(65535, args.control_port))
    args.piper_length_scale = max(0.5, min(2.0, args.piper_length_scale))
    args.piper_noise_scale = max(0.0, min(1.5, args.piper_noise_scale))
    args.piper_noise_w_scale = max(0.0, min(1.5, args.piper_noise_w_scale))
    args.piper_sentence_silence_ms = max(
        0, min(1000, args.piper_sentence_silence_ms)
    )
    return args


def main() -> int:
    args = parse_args()
    args.playback_volume = load_playback_volume(
        args.audio_settings,
        default=args.playback_volume,
    )
    if args.device == "auto":
        args.device = discover_alsa_device("capture", args.audio_device_hint)
    if args.playback_device == "auto":
        args.playback_device = discover_alsa_device(
            "playback", args.audio_device_hint
        )
    audio_setup_ok = False
    if args.playback_backend == "pulse":
        pulse_card_ok = not args.audio_device_hint or configure_pulse_card_volume(
            args.audio_device_hint
        )
        if args.pulse_sink == "auto":
            args.pulse_sink = discover_pulse_sink(
                args.audio_device_hint,
                args.playback_device,
            )
        audio_setup_ok = pulse_card_ok and set_pulse_sink_volume(
            args.pulse_sink,
            args.playback_volume,
        )
    else:
        audio_setup_ok = set_alsa_playback_volume(
            args.audio_device_hint or args.playback_device,
            args.playback_volume,
        )
    bridge = VoiceService(args)
    bridge._emit(
        "audio_setup",
        backend=args.playback_backend,
        volume_percent=args.playback_volume,
        ok=audio_setup_ok,
    )
    try:
        if args.speak_text is not None:
            bridge.speak(args.speak_text)
        elif args.wake_only or args.transcribe_once or args.echo_once:
            bridge.load_models()
            bridge._emit(
                "ready",
                device=args.device,
                playback_backend=args.playback_backend,
                playback_device=args.playback_device,
                pulse_sink=args.pulse_sink or "default",
                wake_phrases=args.wake_phrase,
            )
            if args.echo_once:
                bridge.wait_for_wake_word()
                bridge.play_wake_feedback()
                utterance = bridge.listen_for_utterance(args.command_timeout_sec)
                bridge.speak(f"收到，{utterance}" if utterance else "没听清。")
            elif args.wake_only:
                bridge.wait_for_wake_word()
            else:
                bridge.listen_for_utterance(args.command_timeout_sec)
        else:
            bridge.run_forever()
    except KeyboardInterrupt:
        with contextlib.suppress(BrokenPipeError):
            bridge._emit("stopped")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - systemd needs a clear fatal reason
        print(json.dumps({"event": "fatal", "message": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
