from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "test_microphone_direction.py"
SPEC = importlib.util.spec_from_file_location("test_microphone_direction_script", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_analyze_samples_separates_speech_from_noise() -> None:
    sample_rate = 16000
    rng = np.random.default_rng(7)
    samples = rng.normal(0, 80, sample_rate * 6)
    speech_start = sample_rate * 2
    speech_end = sample_rate * 4
    phase = np.arange(speech_end - speech_start) / sample_rate
    samples[speech_start:speech_end] += np.sin(2 * np.pi * 440 * phase) * 8000

    metrics = MODULE.analyze_samples(samples.astype(np.int16), sample_rate)

    assert metrics["speech_rms_dbfs"] > -16
    assert metrics["noise_floor_dbfs"] < -45
    assert metrics["snr_db"] > 30
    assert metrics["clipping_percent"] == 0
    assert 20 < metrics["active_percent"] < 45


def test_summary_and_report_cover_multiple_directions(tmp_path: Path) -> None:
    rows = []
    for angle, direction, levels, snrs in (
        (0, "front", (-18.0, -17.0, -16.0), (25.0, 26.0, 27.0)),
        (180, "back", (-30.0, -29.0, -28.0), (12.0, 13.0, 14.0)),
    ):
        for repeat, (level, snr) in enumerate(zip(levels, snrs), start=1):
            rows.append(
                {
                    "direction": direction,
                    "angle_deg": str(angle),
                    "distance_cm": "100",
                    "speech_rms_dbfs": str(level),
                    "snr_db": str(snr),
                    "peak_dbfs": str(level + 3),
                    "clipping_percent": "0",
                    "repeat": str(repeat),
                }
            )

    summaries = MODULE.summarize_rows(rows)
    report = tmp_path / "report.png"
    MODULE.render_report(summaries, report)

    assert len(summaries) == 2
    assert summaries[0]["relative_level_db"] == 0
    assert summaries[1]["relative_level_db"] == -12
    assert report.stat().st_size > 1000
