import unittest
import numpy as np
from insight_capture.runtime.mapping.map_store import LandmarkMap, LandmarkMapConfig


class MapStoreTests(unittest.TestCase):
    def test_distinct_confirmed_features_in_one_voxel_are_exported(self):
        landmarks = LandmarkMap(LandmarkMapConfig(voxel_size_m=0.04))
        points = np.array([[0.005, 0.005, 1.005], [0.015, 0.015, 1.015]])
        descriptors = np.eye(2, dtype=np.float32)
        for keyframe in range(1, 4):
            landmarks.update(keyframe, points, descriptors=descriptors)
        xyz, desc = landmarks.descriptors()
        self.assertEqual(landmarks.confirmed_count(), 2)
        np.testing.assert_allclose(xyz, points)
        np.testing.assert_allclose(desc, descriptors)
        self.assertEqual(len(landmarks.descriptors(max_source_keyframe=0)[0]), 0)


if __name__ == '__main__':
    unittest.main()
