import math
import sys
from pathlib import Path

import numpy as np


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from insight9_mapping_core.geometry import PoseSample, matrix_from_pose
from insight9_mapping_core.vio_continuity import (
    VioContinuityConfig,
    VioContinuityStitcher,
)


def _pose(
    stamp_ns: int, x: float, yaw_deg: float = 0.0, y: float = 0.0
) -> PoseSample:
    half_yaw = math.radians(yaw_deg) * 0.5
    return PoseSample(
        stamp_ns=stamp_ns,
        translation=np.array([x, y, 0.0], dtype=np.float64),
        orientation_xyzw=np.array(
            [0.0, 0.0, math.sin(half_yaw), math.cos(half_yaw)],
            dtype=np.float64,
        ),
    )


def _pose_from_planar_matrix(stamp_ns: int, transform: np.ndarray) -> PoseSample:
    yaw_deg = math.degrees(
        math.atan2(float(transform[1, 0]), float(transform[0, 0]))
    )
    return _pose(
        stamp_ns,
        float(transform[0, 3]),
        yaw_deg,
        y=float(transform[1, 3]),
    )


def _push_all(
    stitcher: VioContinuityStitcher,
    samples: list[PoseSample],
    *,
    allow_stitch: bool = True,
) -> list[PoseSample]:
    output = []
    for sample in samples:
        corrected = stitcher.push(sample, allow_stitch=allow_stitch)
        assert len(corrected) == 1
        output.extend(corrected)
    return output


def test_stitches_stationary_translation_reset_without_withholding_pose():
    stitcher = VioContinuityStitcher(VioContinuityConfig())
    samples = [
        *[_pose(index * 10_000_000, 0.0) for index in range(5)],
        *[_pose(index * 10_000_000, 0.08) for index in range(5, 9)],
    ]

    output = _push_all(stitcher, samples)

    assert [sample.stamp_ns for sample in output] == [
        sample.stamp_ns for sample in samples
    ]
    np.testing.assert_allclose(
        [sample.translation[0] for sample in output], 0.0, atol=1e-9
    )
    assert stitcher.stitch_events == 1
    assert stitcher.input_samples == len(samples)
    assert stitcher.output_samples == len(samples)
    assert stitcher.status()["last_event_translation_m"] == 0.08
    assert stitcher.status()["state"] == "tracking"


def test_stitches_moving_translation_and_rotation_reset():
    stitcher = VioContinuityStitcher(VioContinuityConfig())
    desired = [
        _pose(index * 10_000_000, index * 0.004, index * 0.2)
        for index in range(12)
    ]
    raw = desired[:5]
    correction = matrix_from_pose(_pose(0, -0.08, -8.0, y=0.02))
    inverse_correction = np.linalg.inv(correction)
    for sample in desired[5:]:
        raw.append(
            _pose_from_planar_matrix(
                sample.stamp_ns,
                inverse_correction @ matrix_from_pose(sample),
            )
        )

    output = _push_all(stitcher, raw)

    assert len(output) == len(desired)
    for actual, expected in zip(output, desired):
        np.testing.assert_allclose(
            matrix_from_pose(actual), matrix_from_pose(expected), atol=2e-3
        )
    assert stitcher.stitch_events == 1


def test_replaces_correction_after_multiple_coordinate_resets():
    stitcher = VioContinuityStitcher(VioContinuityConfig())
    samples = []
    for index in range(16):
        desired_x = index * 0.002
        raw_offset = 0.0 if index < 4 else (0.07 if index < 10 else -0.06)
        samples.append(_pose(index * 10_000_000, desired_x + raw_offset))

    output = _push_all(stitcher, samples)

    assert len(output) == len(samples)
    np.testing.assert_allclose(
        [sample.translation[0] for sample in output],
        [index * 0.002 for index in range(16)],
        atol=1e-9,
    )
    assert stitcher.stitch_events == 2


def test_constant_fast_motion_is_released_instead_of_stitched():
    config = VioContinuityConfig(translation_threshold_m=0.02)
    stitcher = VioContinuityStitcher(config)
    samples = [_pose(index * 10_000_000, index * 0.04) for index in range(8)]

    output = _push_all(stitcher, samples)

    assert len(output) == len(samples)
    np.testing.assert_allclose(
        [sample.translation[0] for sample in output],
        [sample.translation[0] for sample in samples],
        atol=1e-9,
    )
    assert stitcher.stitch_events == 0


def test_does_not_guess_across_tracking_gap():
    stitcher = VioContinuityStitcher(VioContinuityConfig(max_gap_ms=50.0))
    samples = [
        _pose(0, 0.0),
        _pose(10_000_000, 0.0),
        _pose(20_000_000, 0.0),
        _pose(200_000_000, 0.08),
    ]

    output = _push_all(stitcher, samples)

    assert output[-1].translation[0] == 0.08
    assert stitcher.stitch_events == 0
    assert stitcher.tracking_gaps == 1


def test_timestamp_rollback_starts_a_fresh_local_frame():
    stitcher = VioContinuityStitcher(VioContinuityConfig())
    _push_all(
        stitcher,
        [
            _pose(0, 0.0),
            _pose(10_000_000, 0.0),
            _pose(20_000_000, 0.0),
            _pose(30_000_000, 0.08),
            _pose(40_000_000, 0.08),
            _pose(50_000_000, 0.08),
            _pose(60_000_000, 0.08),
        ],
    )
    assert stitcher.stitch_events == 1

    output = stitcher.push(_pose(5_000_000, 0.25), allow_stitch=True)

    assert len(output) == 1
    assert output[0].translation[0] == 0.25
    assert stitcher.timestamp_resets == 1
    np.testing.assert_allclose(stitcher.correction, np.eye(4), atol=1e-12)


def test_external_gate_suppresses_motion_candidate_without_interrupting_output():
    stitcher = VioContinuityStitcher(VioContinuityConfig())
    initial = _push_all(
        stitcher,
        [
            _pose(0, 0.0),
            _pose(10_000_000, 0.0),
            _pose(20_000_000, 0.0),
        ],
    )

    output = stitcher.push(_pose(30_000_000, 0.08), allow_stitch=False)

    assert len(initial) == 3
    assert len(output) == 1
    assert output[0].translation[0] == 0.08
    assert stitcher.stitch_events == 0
    assert stitcher.rejected_candidates == 1
    np.testing.assert_allclose(stitcher.correction, np.eye(4), atol=1e-12)
