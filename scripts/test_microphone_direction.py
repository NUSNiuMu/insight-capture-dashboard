#!/usr/bin/env python3
"""Measure microphone pickup level one direction at a time and plot a report."""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
import subprocess
import sys
import time
import wave
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

os.environ.setdefault("MPLCONFIGDIR", "/tmp/insight-matplotlib")

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from insight_capture.voice.audio import discover_alsa_device


DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "runs" / "microphone-direction"
VOICE_SERVICE = "insight-voice-control.service"
CSV_FIELDS = [
    "timestamp",
    "session",
    "direction",
    "angle_deg",
    "distance_cm",
    "repeat",
    "duration_sec",
    "sample_rate",
    "device",
    "wav_file",
    "speech_rms_dbfs",
    "noise_floor_dbfs",
    "snr_db",
    "peak_dbfs",
    "clipping_percent",
    "active_percent",
]
SUMMARY_FIELDS = [
    "direction",
    "angle_deg",
    "distance_cm",
    "samples",
    "speech_rms_dbfs_median",
    "speech_rms_dbfs_min",
    "speech_rms_dbfs_max",
    "relative_level_db",
    "snr_db_median",
    "snr_db_min",
    "snr_db_max",
    "peak_dbfs_max",
    "clipping_percent_max",
]
DEFAULT_PHRASE = "宸境，开始录制，检查麦克风收声效果。"


def _dbfs(value: float) -> float:
    return 20.0 * math.log10(max(value, 1e-8))


def analyze_samples(samples: np.ndarray, sample_rate: int) -> dict[str, float]:
    """Estimate speech level and noise from quiet-speech-quiet test audio."""
    if samples.ndim != 1 or samples.size < sample_rate:
        raise ValueError("Recording must contain at least one second of mono audio")

    normalized = samples.astype(np.float64) / 32768.0
    frame_size = max(1, int(sample_rate * 0.02))
    usable = (normalized.size // frame_size) * frame_size
    frames = normalized[:usable].reshape(-1, frame_size)
    frame_rms = np.sqrt(np.mean(np.square(frames), axis=1))
    frame_dbfs = 20.0 * np.log10(np.maximum(frame_rms, 1e-8))

    noise_floor = float(np.percentile(frame_dbfs, 20))
    active_threshold = max(noise_floor + 10.0, -45.0)
    active_mask = frame_dbfs >= active_threshold
    if int(np.count_nonzero(active_mask)) < 3:
        active_mask = frame_dbfs >= float(np.percentile(frame_dbfs, 85))

    active_samples = frames[active_mask].reshape(-1)
    speech_rms = float(np.sqrt(np.mean(np.square(active_samples))))
    speech_rms_dbfs = _dbfs(speech_rms)
    peak = float(np.max(np.abs(normalized)))
    clipping_percent = float(np.mean(np.abs(normalized) >= 0.999) * 100.0)

    return {
        "speech_rms_dbfs": speech_rms_dbfs,
        "noise_floor_dbfs": noise_floor,
        "snr_db": max(0.0, speech_rms_dbfs - noise_floor),
        "peak_dbfs": _dbfs(peak),
        "clipping_percent": clipping_percent,
        "active_percent": float(np.mean(active_mask) * 100.0),
    }


def analyze_wav(path: Path) -> dict[str, float]:
    with wave.open(str(path), "rb") as audio:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2:
            raise ValueError(f"Expected mono 16-bit PCM WAV: {path}")
        sample_rate = audio.getframerate()
        samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2")
    metrics = analyze_samples(samples, sample_rate)
    metrics["sample_rate"] = float(sample_rate)
    return metrics


def voice_service_is_active() -> bool:
    result = subprocess.run(
        ["systemctl", "--user", "is-active", VOICE_SERVICE],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.stdout.strip() in {"active", "activating", "deactivating"}:
        return True
    process = subprocess.run(
        ["pgrep", "-f", r"^python3 -m insight_capture\.voice\.service"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return process.returncode == 0


def record_wav(
    path: Path,
    *,
    device: str,
    duration_sec: int,
    sample_rate: int,
) -> None:
    command = [
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
        "-d",
        str(duration_sec),
        "-t",
        "wav",
        str(path),
    ]
    process = subprocess.Popen(command)
    try:
        print(f"  RECORDING {duration_sec:.0f}s: quiet -> phrase -> quiet", flush=True)
        return_code = process.wait()
    except KeyboardInterrupt:
        process.terminate()
        process.wait()
        path.unlink(missing_ok=True)
        raise
    if return_code != 0:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"arecord failed with exit code {return_code}")


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_rows(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def summarize_rows(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    grouped: dict[float, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[float(row["angle_deg"]) % 360.0].append(row)

    summaries: list[dict[str, Any]] = []
    for angle, group in sorted(grouped.items()):
        levels = np.array([float(row["speech_rms_dbfs"]) for row in group])
        snrs = np.array([float(row["snr_db"]) for row in group])
        peaks = np.array([float(row["peak_dbfs"]) for row in group])
        clipping = np.array([float(row["clipping_percent"]) for row in group])
        summaries.append(
            {
                "direction": group[-1]["direction"],
                "angle_deg": angle,
                "distance_cm": float(group[-1]["distance_cm"]),
                "samples": len(group),
                "speech_rms_dbfs_median": float(np.median(levels)),
                "speech_rms_dbfs_min": float(np.min(levels)),
                "speech_rms_dbfs_max": float(np.max(levels)),
                "snr_db_median": float(np.median(snrs)),
                "snr_db_min": float(np.min(snrs)),
                "snr_db_max": float(np.max(snrs)),
                "peak_dbfs_max": float(np.max(peaks)),
                "clipping_percent_max": float(np.max(clipping)),
            }
        )

    if summaries:
        loudest = max(item["speech_rms_dbfs_median"] for item in summaries)
        for item in summaries:
            item["relative_level_db"] = item["speech_rms_dbfs_median"] - loudest
    return summaries


def render_report(summaries: list[dict[str, Any]], output_path: Path) -> None:
    if not summaries:
        raise ValueError("No measurements are available for the report")

    angles_deg = np.array([item["angle_deg"] for item in summaries])
    angles_rad = np.deg2rad(angles_deg)
    levels = np.array([item["speech_rms_dbfs_median"] for item in summaries])
    relative = levels - float(np.max(levels))
    radial = np.clip(relative, -30.0, 0.0) + 30.0
    snrs = np.array([item["snr_db_median"] for item in summaries])
    labels = [f"{angle:g} deg" for angle in angles_deg]

    fig = plt.figure(figsize=(12, 7), constrained_layout=True)
    grid = fig.add_gridspec(2, 2, width_ratios=(1.15, 1.0))
    polar = fig.add_subplot(grid[:, 0], projection="polar")
    level_ax = fig.add_subplot(grid[0, 1])
    snr_ax = fig.add_subplot(grid[1, 1])

    if len(radial) > 1:
        closed_angles = np.append(angles_rad, angles_rad[0])
        closed_radial = np.append(radial, radial[0])
        polar.plot(closed_angles, closed_radial, color="#1677ff", linewidth=2)
        polar.fill(closed_angles, closed_radial, color="#1677ff", alpha=0.18)
    polar.scatter(angles_rad, radial, color="#0b4f9c", s=55, zorder=3)
    polar.set_theta_zero_location("N")
    polar.set_theta_direction(-1)
    polar.set_ylim(0, 30)
    polar.set_yticks([0, 10, 20, 30])
    polar.set_yticklabels(["-30 dB", "-20 dB", "-10 dB", "0 dB"])
    polar.set_title("Relative pickup level (loudest direction = 0 dB)", pad=22)

    x = np.arange(len(summaries))
    level_low = levels - np.array(
        [item["speech_rms_dbfs_min"] for item in summaries]
    )
    level_high = np.array(
        [item["speech_rms_dbfs_max"] for item in summaries]
    ) - levels
    level_ax.bar(x, levels, color="#1677ff", alpha=0.82)
    level_ax.errorbar(x, levels, yerr=[level_low, level_high], fmt="none", color="black")
    level_ax.set_xticks(x, labels)
    level_ax.set_ylabel("Speech level (dBFS)")
    level_ax.set_title("Captured speech level")
    level_ax.grid(axis="y", alpha=0.25)

    snr_low = snrs - np.array([item["snr_db_min"] for item in summaries])
    snr_high = np.array([item["snr_db_max"] for item in summaries]) - snrs
    snr_ax.bar(x, snrs, color="#18a058", alpha=0.82)
    snr_ax.errorbar(x, snrs, yerr=[snr_low, snr_high], fmt="none", color="black")
    snr_ax.set_xticks(x, labels)
    snr_ax.set_ylabel("Estimated SNR (dB)")
    snr_ax.set_title("Speech-to-background separation")
    snr_ax.grid(axis="y", alpha=0.25)

    fig.suptitle("Microphone directionality test", fontsize=16)
    fig.savefig(output_path, dpi=160)
    plt.close(fig)


def _safe_session(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value) or value in {".", ".."}:
        raise argparse.ArgumentTypeError(
            "session must contain only letters, digits, dot, underscore, or dash"
        )
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record one microphone direction and update a cumulative chart.",
        epilog=(
            f"Stop the microphone owner first: systemctl --user stop {VOICE_SERVICE}. "
            f"Restart it after all directions: systemctl --user start {VOICE_SERVICE}."
        ),
    )
    parser.add_argument("--angle-deg", type=float, help="Clockwise angle; 0 is front")
    parser.add_argument("--direction", help="Human-readable direction label")
    parser.add_argument("--distance-cm", type=float, default=100.0)
    parser.add_argument("--duration-sec", type=int, default=6)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--device-hint", default="E3")
    parser.add_argument("--phrase", default=DEFAULT_PHRASE)
    parser.add_argument("--countdown-sec", type=int, default=3)
    parser.add_argument("--session", type=_safe_session, default="default")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument(
        "--append",
        action="store_true",
        help="Append instead of replacing existing measurements at this angle.",
    )
    parser.add_argument(
        "--no-prompt",
        action="store_true",
        help="Do not wait for Enter before each repeat.",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Regenerate the summary and chart without recording.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    session_dir = args.output_root.expanduser().resolve() / args.session
    wav_dir = session_dir / "wav"
    measurements_path = session_dir / "measurements.csv"
    summary_path = session_dir / "summary.csv"
    chart_path = session_dir / "microphone_direction_report.png"

    if args.report_only:
        rows = read_rows(measurements_path)
        summaries = summarize_rows(rows)
        if not summaries:
            print(f"No measurements found in {session_dir}", file=sys.stderr)
            return 2
        session_dir.mkdir(parents=True, exist_ok=True)
        write_rows(summary_path, summaries, SUMMARY_FIELDS)
        render_report(summaries, chart_path)
        print(f"Chart: {chart_path}")
        return 0

    if args.angle_deg is None:
        print("--angle-deg is required unless --report-only is used", file=sys.stderr)
        return 2
    if args.duration_sec < 3 or args.repeats < 1 or args.sample_rate < 8000:
        print("Use duration >= 3s, repeats >= 1, and sample rate >= 8000", file=sys.stderr)
        return 2
    if voice_service_is_active():
        print(
            f"{VOICE_SERVICE} currently owns the microphone. Stop it before testing:\n"
            f"  systemctl --user stop {VOICE_SERVICE}\n"
            "After all directions, restore it with:\n"
            f"  systemctl --user start {VOICE_SERVICE}",
            file=sys.stderr,
        )
        return 3

    angle = round(args.angle_deg % 360.0, 3)
    direction = args.direction or f"{angle:g}deg"
    device = (
        discover_alsa_device("capture", args.device_hint)
        if args.device == "auto"
        else args.device
    )
    session_dir.mkdir(parents=True, exist_ok=True)
    wav_dir.mkdir(parents=True, exist_ok=True)

    print(f"Direction: {direction} ({angle:g} deg clockwise, 0 deg = front)")
    print(f"Distance: {args.distance_cm:g} cm | Device: {device}")
    print(f"Say once during each recording: {args.phrase}")
    print("Keep about 1 second quiet before and after the phrase.")

    new_rows: list[dict[str, Any]] = []
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", direction).strip("_") or "direction"
    for repeat in range(1, args.repeats + 1):
        if not args.no_prompt:
            input(f"\nPress Enter when ready for repeat {repeat}/{args.repeats}...")
        for remaining in range(args.countdown_sec, 0, -1):
            print(f"  Starting in {remaining}...", flush=True)
            time.sleep(1)
        wav_path = wav_dir / f"{stamp}_{angle:06.1f}deg_{slug}_r{repeat}.wav"
        record_wav(
            wav_path,
            device=device,
            duration_sec=args.duration_sec,
            sample_rate=args.sample_rate,
        )
        metrics = analyze_wav(wav_path)
        row: dict[str, Any] = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "session": args.session,
            "direction": direction,
            "angle_deg": f"{angle:.3f}",
            "distance_cm": f"{args.distance_cm:.1f}",
            "repeat": repeat,
            "duration_sec": f"{args.duration_sec:.1f}",
            "sample_rate": int(metrics.pop("sample_rate")),
            "device": device,
            "wav_file": str(wav_path.relative_to(session_dir)),
        }
        row.update({key: f"{value:.3f}" for key, value in metrics.items()})
        new_rows.append(row)
        print(
            f"  Result: speech {metrics['speech_rms_dbfs']:.1f} dBFS, "
            f"SNR {metrics['snr_db']:.1f} dB, peak {metrics['peak_dbfs']:.1f} dBFS"
        )

    rows = read_rows(measurements_path)
    if not args.append:
        rows = [
            row
            for row in rows
            if not math.isclose(float(row["angle_deg"]) % 360.0, angle, abs_tol=1e-6)
        ]
    rows.extend(new_rows)
    rows.sort(key=lambda row: (float(row["angle_deg"]), row["timestamp"]))
    write_rows(measurements_path, rows, CSV_FIELDS)

    summaries = summarize_rows(rows)
    write_rows(summary_path, summaries, SUMMARY_FIELDS)
    render_report(summaries, chart_path)
    current = next(
        item
        for item in summaries
        if math.isclose(item["angle_deg"], angle, abs_tol=1e-6)
    )
    print(
        f"\nMedian for {direction}: {current['speech_rms_dbfs_median']:.1f} dBFS, "
        f"SNR {current['snr_db_median']:.1f} dB"
    )
    print(f"Measurements: {measurements_path}")
    print(f"Summary:      {summary_path}")
    print(f"Chart:        {chart_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
