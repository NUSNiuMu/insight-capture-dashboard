#!/usr/bin/env python3
"""Recognize a small offline vocabulary from an ALSA capture device."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, Optional


def normalize_transcript(text: object) -> str:
    """Remove recognizer spacing and punctuation before exact phrase matching."""
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(text or "")).lower()


def build_phrase_map(start_phrases: Iterable[str], stop_phrases: Iterable[str]) -> Dict[str, str]:
    phrase_map: Dict[str, str] = {}
    for command, phrases in (("start", start_phrases), ("stop", stop_phrases)):
        for phrase in phrases:
            normalized = normalize_transcript(phrase)
            if not normalized:
                continue
            previous = phrase_map.setdefault(normalized, command)
            if previous != command:
                raise ValueError(f"Voice phrase is assigned to both commands: {phrase}")
    if not any(command == "start" for command in phrase_map.values()):
        raise ValueError("At least one start phrase is required")
    if not any(command == "stop" for command in phrase_map.values()):
        raise ValueError("At least one stop phrase is required")
    return phrase_map


def classify_transcript(text: object, phrase_map: Dict[str, str]) -> Optional[str]:
    return phrase_map.get(normalize_transcript(text))


def segment_phrase_for_model(phrase: str, model) -> str:
    """Find the fewest in-vocabulary words that exactly cover a command."""
    normalized = normalize_transcript(phrase)
    best: list[Optional[list[str]]] = [None] * (len(normalized) + 1)
    best[0] = []
    for end in range(1, len(normalized) + 1):
        for start in range(end):
            prefix = best[start]
            word = normalized[start:end]
            if prefix is None or model.vosk_model_find_word(word) < 0:
                continue
            candidate = prefix + [word]
            if best[end] is None or len(candidate) < len(best[end]):
                best[end] = candidate
    if best[-1] is None:
        raise ValueError(f"Voice phrase is not representable by the Vosk vocabulary: {phrase}")
    return " ".join(best[-1])


def build_arecord_command(device: str, sample_rate: int) -> list[str]:
    return [
        "arecord",
        "-q",
        "-D",
        device,
        "-f",
        "S16_LE",
        "-r",
        str(sample_rate),
        "-c",
        "1",
        "-t",
        "raw",
    ]


def emit(event: str, **payload: object) -> None:
    print(json.dumps({"event": event, **payload}, ensure_ascii=False), flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--chunk-frames", type=int, default=4000)
    parser.add_argument("--cooldown-sec", type=float, default=2.0)
    parser.add_argument("--start-phrase", action="append", default=[])
    parser.add_argument("--stop-phrase", action="append", default=[])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    phrase_map = build_phrase_map(args.start_phrase, args.stop_phrase)
    if not args.model.is_dir():
        raise FileNotFoundError(f"Vosk model directory not found: {args.model}")

    from vosk import KaldiRecognizer, Model, SetLogLevel

    SetLogLevel(-1)
    model = Model(str(args.model))
    grammar_phrases = [segment_phrase_for_model(phrase, model) for phrase in phrase_map]
    grammar = json.dumps(grammar_phrases + ["[unk]"], ensure_ascii=False)
    recognizer = KaldiRecognizer(model, args.sample_rate, grammar)
    capture = subprocess.Popen(
        build_arecord_command(args.device, args.sample_rate),
        stdout=subprocess.PIPE,
        stderr=None,
    )
    if capture.stdout is None:
        raise RuntimeError("ALSA capture stdout is unavailable")

    emit(
        "ready",
        device=args.device,
        sample_rate=args.sample_rate,
        grammar_phrases=grammar_phrases,
    )
    print(
        f"voice control worker ready: device={args.device} grammar={grammar_phrases}",
        file=sys.stderr,
        flush=True,
    )
    last_command_at = 0.0
    try:
        while True:
            audio = capture.stdout.read(args.chunk_frames * 2)
            if not audio:
                return_code = capture.poll()
                raise RuntimeError(f"ALSA capture stopped (exit {return_code})")
            if not recognizer.AcceptWaveform(audio):
                continue
            result = json.loads(recognizer.Result())
            transcript = str(result.get("text", ""))
            command = classify_transcript(transcript, phrase_map)
            if command is None:
                continue
            now = time.monotonic()
            if now - last_command_at < max(0.0, args.cooldown_sec):
                continue
            last_command_at = now
            emit("command", command=command, text=transcript)
    finally:
        capture.terminate()
        try:
            capture.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            capture.kill()
            capture.wait(timeout=2.0)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
    except Exception as exc:  # noqa: BLE001 - surface worker failures to supervisor
        emit("error", message=str(exc))
        print(f"voice control worker: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
