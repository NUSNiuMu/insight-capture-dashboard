import sys
import unittest
from pathlib import Path

import numpy as np


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from insight9_mapping_core.global_localization import (
    GlobalLocalizationConfig,
    LocalizationCandidate,
    LocalizationConsensus,
    associate_reference_points,
    localize_correspondences,
)


def _candidate(x: float, yaw_deg: float = 0.0) -> LocalizationCandidate:
    radians = np.deg2rad(yaw_deg)
    cosine, sine = np.cos(radians), np.sin(radians)
    transform = np.eye(4, dtype=np.float64)
    transform[:2, :2] = ((cosine, -sine), (sine, cosine))
    transform[0, 3] = x
    return LocalizationCandidate(
        map_to_camera=transform.copy(),
        map_to_odom=transform,
        matches=20,
        inliers=15,
        inlier_ratio=0.75,
        median_reprojection_error_px=0.5,
        grid_cells=6,
    )


class GlobalLocalizationTest(unittest.TestCase):
    def test_consensus_accepts_three_consistent_candidates_among_five_attempts(self):
        consensus = LocalizationConsensus(GlobalLocalizationConfig())

        self.assertEqual(
            consensus.observe(_candidate(0.00))["confirmation_progress"], 1
        )
        self.assertEqual(consensus.observe(None)["confirmation_progress"], 1)
        self.assertEqual(
            consensus.observe(_candidate(0.02))["confirmation_progress"], 2
        )
        self.assertEqual(consensus.observe(None)["confirmation_progress"], 2)
        result = consensus.observe(_candidate(0.01))

        self.assertTrue(result["localized"])
        self.assertEqual(result["confirmation_progress"], 0)
        self.assertEqual(result["confirmation_window"], 5)
        np.testing.assert_allclose(consensus.correction[:3, 3], (0.01, 0.0, 0.0))

    def test_consensus_drops_candidates_that_age_out_of_five_attempt_window(self):
        consensus = LocalizationConsensus(GlobalLocalizationConfig())

        consensus.observe(_candidate(0.00))
        consensus.observe(_candidate(0.01))
        for _ in range(4):
            result = consensus.observe(None)

        self.assertFalse(result["localized"])
        self.assertEqual(result["confirmation_progress"], 1)

    def test_consensus_ignores_inconsistent_candidate_inside_window(self):
        consensus = LocalizationConsensus(GlobalLocalizationConfig())

        consensus.observe(_candidate(0.00))
        consensus.observe(_candidate(1.00, 45.0))
        consensus.observe(None)
        consensus.observe(_candidate(0.02))
        result = consensus.observe(_candidate(0.01))

        self.assertTrue(result["localized"])
        self.assertEqual(consensus.correction[0, 3], 0.01)

    def test_associate_reference_points_rejects_unmapped_and_duplicate_matches(self):
        reference_pixels = np.array(
            [[10.0, 20.0], [30.0, 40.0], [50.0, 60.0]], dtype=np.float32
        )
        reference_objects = np.array(
            [[0.0, 0.0, 1.0], [1.0, 0.0, 1.0], [2.0, 0.0, 1.0]],
            dtype=np.float32,
        )
        query_pixels = np.array(
            [[100.0, 120.0], [101.0, 121.0], [130.0, 140.0], [150.0, 160.0]],
            dtype=np.float32,
        )
        matched_reference = np.array(
            [[10.4, 20.0], [10.1, 20.0], [30.2, 39.9], [80.0, 90.0]],
            dtype=np.float32,
        )

        image_points, object_points = associate_reference_points(
            query_pixels,
            matched_reference,
            reference_pixels,
            reference_objects,
        )

        np.testing.assert_allclose(image_points, [[101.0, 121.0], [130.0, 140.0]])
        np.testing.assert_allclose(object_points, reference_objects[:2])

    def test_localize_correspondences_recovers_synthetic_camera_pose(self):
        object_points = np.array(
            [
                [-0.4, -0.3, 2.0],
                [0.4, -0.3, 2.1],
                [-0.4, 0.3, 2.2],
                [0.4, 0.3, 2.3],
                [-0.7, -0.1, 3.0],
                [0.7, -0.1, 3.1],
                [-0.7, 0.4, 3.2],
                [0.7, 0.4, 3.3],
                [-0.2, -0.6, 2.6],
                [0.2, 0.6, 2.8],
                [-0.8, 0.0, 4.0],
                [0.8, 0.0, 4.2],
            ],
            dtype=np.float32,
        )
        camera_matrix = np.array(
            [[500.0, 0.0, 320.0], [0.0, 500.0, 240.0], [0.0, 0.0, 1.0]],
            dtype=np.float64,
        )
        image_points = np.column_stack(
            (
                camera_matrix[0, 0] * object_points[:, 0] / object_points[:, 2]
                + camera_matrix[0, 2],
                camera_matrix[1, 1] * object_points[:, 1] / object_points[:, 2]
                + camera_matrix[1, 2],
            )
        ).astype(np.float32)
        config = GlobalLocalizationConfig(
            min_matches=8,
            min_inliers=8,
            min_inlier_ratio=0.5,
            min_grid_cells=3,
        )

        candidate, diagnostics = localize_correspondences(
            image_points,
            object_points,
            camera_matrix,
            np.eye(4),
            (480, 640),
            config,
        )

        self.assertTrue(diagnostics["accepted"])
        self.assertIsNotNone(candidate)
        np.testing.assert_allclose(candidate.map_to_camera, np.eye(4), atol=1e-4)


if __name__ == "__main__":
    unittest.main()
