"""Local speech synthesis helpers."""

import subprocess
import wave
from pathlib import Path


def synthesize_espeak(binary: str, voice: str, speed: int, text: str, output_path: Path) -> None:
    subprocess.run([binary, "-v", voice, "-s", str(speed), "-w", str(output_path), "--", text], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def synthesize_piper_wav(
    voice,
    text: str,
    output_path: Path,
    synthesis_config,
    *,
    sentence_silence_ms: int,
) -> None:
    """Write Piper sentence chunks with an explicit pause between them."""
    chunks = iter(voice.synthesize(text, syn_config=synthesis_config))
    first = next(chunks, None)
    with wave.open(str(output_path), "wb") as wav_file:
        if first is None:
            wav_file.setframerate(22050)
            wav_file.setsampwidth(2)
            wav_file.setnchannels(1)
            return
        wav_file.setframerate(first.sample_rate)
        wav_file.setsampwidth(first.sample_width)
        wav_file.setnchannels(first.sample_channels)
        wav_file.writeframes(first.audio_int16_bytes)
        silence_frames = round(first.sample_rate * sentence_silence_ms / 1000)
        silence = b"\0" * (
            silence_frames * first.sample_width * first.sample_channels
        )
        for chunk in chunks:
            if silence:
                wav_file.writeframes(silence)
            wav_file.writeframes(chunk.audio_int16_bytes)
