import unittest
import sys
from pathlib import Path

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from insight_capture.runtime.mapping.bundle_adjustment import (
    BundleAdjustmentConfig,
    BundleAdjustmentFrame,
    optimize_local_bundle,
)
from insight_capture.runtime.mapping.pose_graph import KeyframePoseGraph


def _projection_matrices() -> tuple[np.ndarray, np.ndarray]:
    focal = 420.0
    center_x, center_y = 320.0, 272.0
    baseline = 0.10
    left = np.array(
        [[focal, 0.0, center_x, 0.0], [0.0, focal, center_y, 0.0], [0.0, 0.0, 1.0, 0.0]],
        dtype=np.float64,
    )
    right = left.copy()
    right[0, 3] = -focal * baseline
    return left, right


def _project(points_left: np.ndarray, projection: np.ndarray) -> np.ndarray:
    homogeneous = np.concatenate(
        (points_left, np.ones((len(points_left), 1), dtype=np.float64)), axis=1
    )
    projected = homogeneous @ projection.T
    return projected[:, :2] / projected[:, 2:3]


class BundleAdjustmentTests(unittest.TestCase):
    def test_jointly_reduces_pose_drift_and_reprojection_error(self) -> None:
        random = np.random.default_rng(9)
        left_projection, right_projection = _projection_matrices()
        landmarks = np.column_stack(
            (
                random.uniform(-0.35, 0.35, 24),
                random.uniform(-0.25, 0.25, 24),
                random.uniform(1.8, 3.0, 24),
            )
        )
        descriptors = random.normal(size=(len(landmarks), 32)).astype(np.float32)
        descriptors /= np.linalg.norm(descriptors, axis=1, keepdims=True)
        frames = []
        true_poses = []
        for frame_index in range(6):
            true_pose = np.eye(4, dtype=np.float64)
            true_pose[0, 3] = frame_index * 0.055
            true_pose[1, 3] = frame_index * 0.008
            true_poses.append(true_pose)
            points_left = landmarks - true_pose[:3, 3]
            left_pixels = _project(points_left, left_projection)
            right_pixels = _project(points_left, right_projection)
            noisy_pose = true_pose.copy()
            noisy_pose[0, 3] += frame_index * 0.008
            noisy_pose[1, 3] -= frame_index * 0.004
            frames.append(
                BundleAdjustmentFrame(
                    keyframe_id=frame_index + 1,
                    pose=noisy_pose,
                    points_left=points_left.astype(np.float32),
                    left_pixels=left_pixels.astype(np.float32),
                    right_pixels=right_pixels.astype(np.float32),
                    descriptors=descriptors,
                    scores=np.ones((len(landmarks),), dtype=np.float32),
                )
            )

        result = optimize_local_bundle(
            frames,
            left_projection,
            right_projection,
            BundleAdjustmentConfig(
                max_points_per_keyframe=100,
                max_landmarks=100,
                max_iterations=30,
                min_relative_improvement=0.001,
            ),
        )

        self.assertTrue(result.optimized, result.reason)
        self.assertTrue(result.success)
        self.assertGreater(result.landmarks, 15)
        self.assertLess(
            result.final_reprojection_rmse_px,
            result.initial_reprojection_rmse_px * 0.25,
        )
        initial_error = np.linalg.norm(
            frames[-1].pose[:3, 3] - true_poses[-1][:3, 3]
        )
        final_error = np.linalg.norm(
            result.poses[frames[-1].keyframe_id][:3, 3]
            - true_poses[-1][:3, 3]
        )
        self.assertLess(final_error, initial_error * 0.75)

    def test_rejects_window_without_repeated_descriptors(self) -> None:
        left_projection, right_projection = _projection_matrices()
        frames = []
        for frame_index in range(4):
            descriptor = np.zeros((1, 8), dtype=np.float32)
            descriptor[0, frame_index] = 1.0
            frames.append(
                BundleAdjustmentFrame(
                    keyframe_id=frame_index + 1,
                    pose=np.eye(4),
                    points_left=np.array([[0.0, 0.0, 2.0]], dtype=np.float32),
                    left_pixels=np.array([[320.0, 272.0]], dtype=np.float32),
                    right_pixels=np.array([[299.0, 272.0]], dtype=np.float32),
                    descriptors=descriptor,
                    scores=np.ones((1,), dtype=np.float32),
                )
            )

        result = optimize_local_bundle(
            frames, left_projection, right_projection, BundleAdjustmentConfig()
        )

        self.assertFalse(result.optimized)
        self.assertEqual(result.reason, "insufficient_repeated_landmarks")

    def test_pose_graph_accepts_validated_external_pose_solution(self) -> None:
        graph = KeyframePoseGraph()
        first = np.eye(4, dtype=np.float64)
        second = np.eye(4, dtype=np.float64)
        second[0, 3] = 0.10
        graph.add_keyframe(1, 1_000, first)
        graph.add_keyframe(2, 2_000, second)
        refined = second.copy()
        refined[1, 3] = 0.02

        graph.apply_pose_updates({2: refined})

        self.assertEqual(graph.keyframe_ids(), (1, 2))
        np.testing.assert_allclose(graph.pose(2), refined)
        with self.assertRaises(KeyError):
            graph.apply_pose_updates({3: np.eye(4)})


if __name__ == "__main__":
    unittest.main()
