"""ALSA device discovery, capture, and wake-tone generation."""

import io
import math
import struct
import subprocess
import wave
from typing import Optional


def wake_tone_wav(sample_rate: int, duration_ms: int, frequency_hz: int, volume: float) -> bytes:
    frame_count = max(1, round(sample_rate * duration_ms / 1000))
    fade_frames = max(1, min(frame_count // 2, round(sample_rate * 0.02)))
    pcm = bytearray()
    for index in range(frame_count):
        envelope = min(1.0, index / fade_frames, (frame_count - index) / fade_frames)
        sample = int(32767 * volume * envelope * math.sin(2.0 * math.pi * frequency_hz * index / sample_rate))
        pcm.extend(struct.pack("<h", sample))
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return output.getvalue()


def discover_alsa_device(kind: str, preferred: str = "E3") -> str:
    binary = "arecord" if kind == "capture" else "aplay"
    try:
        completed = subprocess.run([binary, "-L"], check=False, capture_output=True, text=True, timeout=3.0)
    except (OSError, subprocess.SubprocessError):
        return "default"
    candidates = [line.strip() for line in completed.stdout.splitlines() if line and not line[0].isspace()]
    stable = [name for name in candidates if name.startswith("plughw:CARD=")]
    hint = str(preferred or "").casefold()
    return next((name for name in stable if hint and hint in name.casefold()), stable[0] if stable else "default")


class AlsaCapture:
    def __init__(self, device: str, sample_rate: int, chunk_frames: int) -> None:
        self.device, self.sample_rate, self.chunk_frames = device, sample_rate, chunk_frames
        self.process: Optional[subprocess.Popen] = None

    def __enter__(self):
        self.process = subprocess.Popen(["arecord", "-q", "-D", self.device, "-f", "S16_LE", "-r", str(self.sample_rate), "-c", "1", "-t", "raw"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
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
