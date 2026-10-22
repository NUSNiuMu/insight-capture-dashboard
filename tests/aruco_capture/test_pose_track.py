import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from insight_capture.aruco_capture.pose_track import (
    pose_settings,
    smooth_arm,
    update_pose_settings,
)


def _block(positions, rotations):
    block = np.zeros((len(positions), 9), np.float32)
    block[:, :3] = positions
    block[:, 3:9] = rotations.as_matrix()[:, :2].reshape(-1, 6)
    return block


class SmoothArmTests(unittest.TestCase):
    def test_single_frame_flip_is_removed_and_slow_motion_kept(self):
        fps, count = 30, 90
        t = np.arange(count) / fps
        positions = np.c_[0.1 * np.sin(2 * np.pi * 0.5 * t), np.zeros(count), np.zeros(count)]
        angles = np.zeros((count, 3))
        angles[:, 2] = 20 * np.sin(2 * np.pi * 0.5 * t)
        angles[45] += [8, 0, 0]  # a single-face PnP flip
        block = _block(positions, Rotation.from_euler("xyz", angles, degrees=True))
        clean = _block(
            positions,
            Rotation.from_euler("xyz", np.c_[np.zeros(count), np.zeros(count), angles[:, 2]], degrees=True),
        )

        smooth_arm(block, np.ones(count, bool), fps, 6.0)

        rows = block[:, 3:9].reshape(-1, 2, 3)
        recovered = Rotation.from_matrix(
            np.concatenate([rows, np.cross(rows[:, 0], rows[:, 1])[:, None]], 1)
        )
        reference = Rotation.from_matrix(
            np.concatenate(
                [clean[:, 3:9].reshape(-1, 2, 3), np.cross(clean[:, 3:6], clean[:, 6:9])[:, None]], 1
            )
        )
        self.assertLess(np.degrees((recovered[45] * reference[45].inv()).magnitude()), 1.0)
        self.assertLess(np.abs(block[:, :3] - positions).max(), 0.002)

    def test_zero_cutoff_and_gaps_leave_samples_untouched(self):
        rng = np.random.default_rng(0)
        positions = rng.normal(0, 0.01, (40, 3))
        block = _block(positions, Rotation.identity(40))
        original = block.copy()
        smooth_arm(block, np.ones(40, bool), 30, 0)
        np.testing.assert_array_equal(block, original)

        mask = np.ones(40, bool)
        mask[20] = False
        smooth_arm(block, mask, 30, 6.0)
        np.testing.assert_array_equal(block[20], original[20])


class PoseSettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name)
        (self.path / "session.json").write_text(
            json.dumps({"config": {"fps": 30, "pose_filter_hz": 5}})
        )

    def tearDown(self):
        self.directory.cleanup()

    def test_defaults_follow_session_config(self):
        settings = pose_settings(self.path)
        self.assertEqual(settings["source"], "live")
        self.assertEqual(settings["filter_hz"], 5.0)
        self.assertIsNone(settings["offline"])

    def test_rejects_missing_offline_result_and_aliased_cutoff(self):
        with self.assertRaises(ValueError):
            update_pose_settings(self.path, {"source": "offline"})
        with self.assertRaises(ValueError):
            update_pose_settings(self.path, {"filter_hz": 15})
        self.assertEqual(update_pose_settings(self.path, {"filter_hz": 0})["filter_hz"], 0)


if __name__ == "__main__":
    unittest.main()
