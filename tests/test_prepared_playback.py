from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from post_processing_core.prepared_playback import (  # noqa: E402
    PreparedPlaybackManager,
    _cache_key,
    _nearest_indices,
    _playback_frame,
)


class PreparedPlaybackTest(unittest.TestCase):
    def test_cache_key_includes_playback_schema(self) -> None:
        signature = [{"name": "bag.db3", "size": 10, "mtime_ns": 20}]
        configuration = {"cameras": [{"name": "camera"}], "poses": []}
        self.assertEqual(
            _cache_key(signature, configuration),
            _cache_key(signature, configuration),
        )
        self.assertNotEqual(
            _cache_key(signature, configuration),
            _cache_key(signature, {"cameras": [], "poses": []}),
        )

    def test_playback_frame_bounds_large_images_with_even_dimensions(self) -> None:
        source = np.zeros((1920, 1088, 3), dtype=np.uint8)
        output = _playback_frame(source)
        self.assertEqual(output.shape, (720, 408, 3))

        small = np.zeros((640, 544, 3), dtype=np.uint8)
        self.assertIs(_playback_frame(small), small)

    def test_nearest_indices_preserve_missing_frame_as_duplicate(self) -> None:
        source = np.asarray([0, 33_000_000, 100_000_000], dtype=np.int64)
        target = np.asarray([0, 33_333_333, 66_666_667, 100_000_000], dtype=np.int64)
        indices, skew_ms = _nearest_indices(source, target)
        self.assertEqual(indices.tolist(), [0, 1, 2, 2])
        self.assertAlmostEqual(float(skew_ms[2]), 33.333333, places=5)

    def test_pose_manifest_is_fixed_rate_and_bounded_trajectory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = PreparedPlaybackManager(root, root / "cache")
            count = 900
            stamps = np.arange(count, dtype=np.int64) * 10_000_000
            positions = np.column_stack(
                (np.arange(count, dtype=np.float64), np.zeros(count), np.zeros(count))
            )
            quaternions = np.tile([0.0, 0.0, 0.0, 1.0], (count, 1))
            targets = np.arange(270, dtype=np.int64) * 33_333_333
            payload = manager._pose_manifest(
                {"camera": (stamps, positions, quaternions)},
                [{"name": "camera", "role": "head"}],
                targets,
            )[0]
        self.assertEqual(len(payload["positions"]), len(targets))
        self.assertEqual(len(payload["quaternions_xyzw"]), len(targets))
        self.assertLessEqual(len(payload["trajectory"]), 600)
        self.assertTrue(all(payload["valid"]))


if __name__ == "__main__":
    unittest.main()
