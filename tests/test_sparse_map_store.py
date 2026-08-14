import sys
import unittest
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from insight9_mapping_core.map_store import LandmarkMap, LandmarkMapConfig  # noqa: E402


class LandmarkMapObservationTest(unittest.TestCase):
    def setUp(self):
        self.landmarks = LandmarkMap(
            LandmarkMapConfig(
                voxel_size_m=0.04,
                confirmation_observations=3,
                candidate_ttl_keyframes=12,
            )
        )
        self.points = np.asarray([[0.12, -0.08, 1.0]], dtype=np.float32)
        self.descriptors = np.asarray([[1.0, 0.0]], dtype=np.float32)

    def update(self, observation_id: int):
        return self.landmarks.update(
            1,
            self.points,
            observation_id=observation_id,
            descriptors=self.descriptors,
        )

    def test_stationary_observations_confirm_without_new_keyframes(self):
        self.assertEqual(self.update(1).confirmed_total, 0)
        self.assertEqual(self.update(2).confirmed_total, 0)
        result = self.update(3)
        self.assertEqual(result.promoted, 1)
        self.assertEqual(result.confirmed_total, 1)

    def test_duplicate_observation_is_not_counted_twice(self):
        self.update(1)
        self.update(1)
        self.assertEqual(self.update(2).confirmed_total, 0)
        self.assertEqual(self.update(3).confirmed_total, 1)

    def test_source_keyframe_filter_remains_independent_from_observation_id(self):
        self.update(10)
        self.update(11)
        self.update(12)
        points, _descriptors = self.landmarks.descriptors(max_source_keyframe=0)
        self.assertEqual(len(points), 0)
        points, _descriptors = self.landmarks.descriptors(max_source_keyframe=1)
        self.assertEqual(len(points), 1)


if __name__ == "__main__":
    unittest.main()
