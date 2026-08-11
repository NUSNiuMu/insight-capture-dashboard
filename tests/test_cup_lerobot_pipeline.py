from __future__ import annotations

from pathlib import Path
import json
import sys
import tempfile

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from cup_lerobot_pipeline import (  # noqa: E402
    ATOMIC_LABELS,
    CupEpisodeSelector,
    infer_atomic_boundaries,
    rebalance_short_segments,
    update_cup_catalog,
)
from lerobot_dataset_export import (  # noqa: E402
    gripper_width_quality,
    gripper_width_valid_mask,
)
from umi_dataset_export import EpisodePlan  # noqa: E402


FPS = 20.0


def _complete_width_trace() -> np.ndarray:
    values = np.full(240, 0.083, dtype=np.float32)
    values[60:72] = np.linspace(0.083, 0.01, 12)
    values[72:150] = 0.01
    values[150:164] = np.linspace(0.01, 0.083, 14)
    return values


def _plan(width: np.ndarray, *, source_segment_index: int = 0) -> EpisodePlan:
    count = len(width)
    timestamps = np.arange(count, dtype=np.int64) * int(1e9 / FPS)
    return EpisodePlan(
        bag_name="test_bag",
        timestamps_ns=timestamps,
        image_indices={"camera": np.arange(count)},
        image_timestamps_ns={"camera": timestamps.copy()},
        image_valid={"camera": np.ones(count, dtype=bool)},
        lowdim={"robot0_gripper_width": width[:, None]},
        detection_rates={"camera": 1.0},
        max_image_skew_ms=0.0,
        pose_quality_events={"camera": {}},
        segmentation={
            "mode": "auto_pause",
            "source_segment_index": source_segment_index,
            "source_start_s": 10.0,
            "source_end_s": 22.0,
            "rejected_segments": [],
        },
    )


def test_atomic_boundaries_cover_complete_open_close_open_cycle() -> None:
    boundaries, metrics = infer_atomic_boundaries(
        _complete_width_trace(), fps=FPS
    )
    assert len(boundaries) == len(ATOMIC_LABELS) + 1
    assert boundaries[0] == 0
    assert boundaries[-1] == 240
    assert all(right > left for left, right in zip(boundaries, boundaries[1:]))
    assert min(np.diff(boundaries)) >= 10
    assert boundaries[1] >= 59
    assert boundaries[2] >= 149
    assert boundaries[3] <= 165
    assert metrics["range_m"] > 0.07


def test_short_false_opening_is_not_mistaken_for_release() -> None:
    width = _complete_width_trace()
    width[100:108] = 0.083
    boundaries, _metrics = infer_atomic_boundaries(width, fps=FPS)
    assert boundaries[2] >= 149


def test_short_segments_are_rebalanced_to_openpi_chunk_length() -> None:
    boundaries = rebalance_short_segments([0, 30, 32, 58, 64])
    assert boundaries[0] == 0
    assert boundaries[-1] == 64
    assert min(np.diff(boundaries)) == 10


def test_gripper_spike_is_marked_without_invalidating_recovery() -> None:
    widths = np.asarray([0.08, 0.079, 0.0, 0.079, 0.078], dtype=np.float32)
    valid = gripper_width_valid_mask(widths)
    quality = gripper_width_quality(widths)
    assert valid.tolist() == [True, True, False, True, True]
    assert quality["jump_events"] == 2
    assert quality["invalid_frames"] == 1


def test_gripper_large_step_marks_landing_sample_invalid() -> None:
    widths = np.asarray([0.08, 0.079, 0.01, 0.011], dtype=np.float32)
    assert gripper_width_valid_mask(widths).tolist() == [True, True, False, True]


@pytest.mark.parametrize(
    "width",
    (
        np.full(100, 0.083, dtype=np.float32),
        np.full(100, 0.01, dtype=np.float32),
    ),
)
def test_incomplete_gripper_cycle_is_rejected(width: np.ndarray) -> None:
    with pytest.raises(ValueError):
        infer_atomic_boundaries(width, fps=FPS)


def test_selector_trims_context_and_rejects_idle_segments() -> None:
    selector = CupEpisodeSelector(
        fps=FPS,
        minimum_frames=24,
        pre_roll_s=2.0,
        post_roll_s=2.5,
    )
    selected = selector(
        Path("test_bag"),
        [
            _plan(_complete_width_trace(), source_segment_index=0),
            _plan(np.full(100, 0.083, dtype=np.float32), source_segment_index=1),
        ],
    )
    assert len(selected) == 1
    episode = selected[0]
    assert episode.segmentation["mode"] == "cup_grasp"
    assert episode.segmentation["source_start_s"] > 10.0
    assert episode.segmentation["source_end_s"] < 22.0
    assert len(episode.segmentation["atomic_boundaries"]) == 5
    assert min(np.diff(episode.segmentation["atomic_boundaries"])) >= 10
    assert selector.report()["accepted_episode_count"] == 1
    assert len(selector.rejected) == 1


def test_catalog_counts_verified_cup_datasets_only() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for name, episodes in (("first", 12), ("second", 8)):
            manifest = root / name / "meta/manifest.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps(
                    {
                        "dataset_id": f"test/{name}",
                        "episode_count": episodes,
                        "total_frames": episodes * 100,
                        "duration_s": episodes * 5,
                        "size_bytes": episodes * 1000,
                        "quality_filtering": {
                            "mode": "cup_grasp",
                            "source_bag": f"{name}_bag",
                        },
                        "verification": {"status": "PASS"},
                    }
                ),
                encoding="utf-8",
            )
        ignored = root / "ordinary/meta/manifest.json"
        ignored.parent.mkdir(parents=True)
        ignored.write_text(json.dumps({"episode_count": 99}), encoding="utf-8")
        catalog_path = update_cup_catalog(root)
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    assert catalog["dataset_count"] == 2
    assert catalog["episode_count"] == 20
    assert catalog["remaining_episode_count"] == 480
