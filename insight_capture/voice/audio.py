"""ALSA device discovery, capture, and wake-tone generation."""

import io
import math
import re
import shlex
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


def _alsa_devices(kind: str) -> list[tuple[str, str]]:
    binary = "arecord" if kind == "capture" else "aplay"
    try:
        completed = subprocess.run([binary, "-L"], check=False, capture_output=True, text=True, timeout=3.0)
    except (OSError, subprocess.SubprocessError):
        return []

    devices: list[tuple[str, str]] = []
    current = ""
    description: list[str] = []
    for line in completed.stdout.splitlines():
        if line and not line[0].isspace():
            if current:
                devices.append((current, " ".join(description)))
            name = line.strip()
            current = name if name.startswith("plughw:CARD=") else ""
            description = []
        elif current and line.strip():
            description.append(line.strip())
    if current:
        devices.append((current, " ".join(description)))
    return devices


def _alsa_card(device: str) -> str:
    match = re.search(r"(?:^|[:,])CARD=([^,]+)", device)
    return match.group(1) if match else ""


def _select_alsa_device(
    devices: list[tuple[str, str]], preferred: str = ""
) -> str:
    hint = str(preferred or "").casefold()
    if hint:
        matched = next(
            (
                name
                for name, description in devices
                if hint in f"{name} {description}".casefold()
            ),
            "",
        )
        if matched:
            return matched
    return next(
        (name for name, description in devices if "usb" in description.casefold()),
        devices[0][0] if devices else "default",
    )


def discover_alsa_device(kind: str, preferred: str = "") -> str:
    return _select_alsa_device(_alsa_devices(kind), preferred)


def list_alsa_devices(kind: str) -> list[dict[str, str]]:
    """List one stable device per currently scanned USB sound card."""
    devices = _alsa_devices(kind)
    usb_devices = [
        device for device in devices if "usb" in device[1].casefold()
    ]
    candidates = usb_devices or devices
    options: list[dict[str, str]] = []
    seen_cards: set[str] = set()
    for device, description in candidates:
        card = _alsa_card(device)
        if not card or card in seen_cards:
            continue
        seen_cards.add(card)
        label = description.split(", USB Audio", 1)[0].strip() or card
        options.append({"id": device, "card": card, "label": label})
    return options


def discover_pulse_sink(preferred: str = "", playback_device: str = "") -> str:
    """Resolve a live PulseAudio sink, honoring an optional operator hint."""
    try:
        completed = subprocess.run(
            ["pactl", "list", "short", "sinks"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3.0,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    sinks = []
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2:
            sinks.append(fields[1])
    hint = str(preferred or "").casefold()
    matched = next(
        (name for name in sinks if hint and hint in name.casefold()),
        "",
    )
    if matched:
        return matched
    card_key = re.sub(r"[^a-z0-9]+", "", _alsa_card(playback_device).casefold())
    if len(card_key) >= 3:
        matched = next(
            (
                name
                for name in sinks
                if card_key in re.sub(r"[^a-z0-9]+", "", name.casefold())
            ),
            "",
        )
        if matched:
            return matched
    try:
        default = subprocess.run(
            ["pactl", "get-default-sink"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3.0,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        default = ""
    if default in sinks:
        return default
    return next(
        (name for name in sinks if name.startswith("alsa_output.usb-")),
        sinks[0] if sinks else "",
    )


def configure_pulse_card_volume(preferred: str = "") -> bool:
    """Ignore a matching USB card's invalid dB metadata without affecting other cards."""
    try:
        completed = subprocess.run(
            ["pactl", "list", "short", "modules"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3.0,
        )
    except (OSError, subprocess.SubprocessError):
        return False

    hint = str(preferred or "").casefold()
    module_index = ""
    original_args: list[str] = []
    for line in completed.stdout.splitlines():
        fields = line.split(maxsplit=2)
        if len(fields) < 3 or fields[1] != "module-alsa-card":
            continue
        if not hint or hint not in fields[2].casefold():
            continue
        module_index = fields[0]
        try:
            original_args = shlex.split(fields[2])
        except ValueError:
            return False
        break
    if not module_index or not original_args:
        return False

    patched_args = []
    ignore_db_found = False
    already_ignored = False
    for argument in original_args:
        if argument.startswith("ignore_dB="):
            ignore_db_found = True
            already_ignored = argument.split("=", 1)[1].casefold() in {
                "1",
                "true",
                "yes",
            }
            patched_args.append("ignore_dB=yes")
        else:
            patched_args.append(argument)
    if already_ignored:
        return True
    if not ignore_db_found:
        patched_args.append("ignore_dB=yes")

    unloaded = False
    try:
        subprocess.run(
            ["pactl", "unload-module", module_index],
            check=True,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        unloaded = True
        subprocess.run(
            ["pactl", "load-module", "module-alsa-card", *patched_args],
            check=True,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        # The udev-loaded card is more useful than no PulseAudio card if the
        # device-specific workaround is unsupported on another host.
        if unloaded:
            try:
                subprocess.run(
                    ["pactl", "load-module", "module-alsa-card", *original_args],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        return False
    return True


def set_pulse_sink_volume(sink: str, volume_percent: int) -> bool:
    """Restore the shared playback sink to a predictable startup volume."""
    if not sink:
        return False
    try:
        completed = subprocess.run(
            ["pactl", "set-sink-volume", sink, f"{volume_percent}%"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3.0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def set_alsa_playback_volume(card: str, volume_percent: int) -> bool:
    """Restore direct ALSA playback volume when PulseAudio is disabled."""
    card = _alsa_card(card) or card
    if not card:
        return False
    try:
        completed = subprocess.run(
            [
                "amixer",
                "-q",
                "-c",
                str(card),
                "sset",
                "PCM",
                f"{volume_percent}%",
                "unmute",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=3.0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


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
