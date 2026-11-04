import json
import unittest
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from insight_capture.aruco_capture.pose import DirectPose, gravity_frame
from insight_capture.runtime.mapping.geometry import PoseSample

CONFIG = Path(__file__).resolve().parents[2] / "config/devices/x86-aruco/capture.json"


class GravityFrameTest(unittest.TestCase):
    def test_axes_follow_gravity_and_camera_heading(self):
        up = np.array([-0.59, 0.03, -0.81])
        forward = np.array([0.0, 0.0, 1.0])
        rotation = gravity_frame(up, forward)[:3, :3]
        np.testing.assert_allclose(rotation @ rotation.T, np.eye(3), atol=1e-12)
        self.assertAlmostEqual(np.linalg.det(rotation), 1.0)
        np.testing.assert_allclose(
            rotation @ (up / np.linalg.norm(up)), [0, 0, 1], atol=1e-12
        )
        heading = rotation @ forward
        self.assertAlmostEqual(heading[0], 0.0)
        self.assertGreater(heading[1], 0.0)
        np.testing.assert_allclose(np.cross(rotation[1], rotation[2]), rotation[0])

    def test_vertical_optical_axis_is_rejected(self):
        with self.assertRaises(ValueError):
            gravity_frame([0, 0, 1], [0, 0.01, -1])


class ReferenceTest(unittest.TestCase):
    def setUp(self):
        self.pose = DirectPose(json.loads(CONFIG.read_text()))
        self.imu_from_rgb = np.eye(4)

    def test_vio_world_z_becomes_up(self):
        world_from_imu = Rotation.from_euler("xyz", [30, -50, 70], degrees=True)
        head = PoseSample(0, np.array([0.1, 0.2, 0.3]), world_from_imu.as_quat())
        self.pose.observe(None, np.eye(3), head, self.imu_from_rgb, 0.0, {})
        world_up = self.pose.reference[:3, :3] @ [0, 0, 1]
        np.testing.assert_allclose(world_up, [0, 0, 1], atol=1e-9)
        camera = self.pose.reference_camera
        self.assertAlmostEqual(camera[0, 2], 0.0)
        np.testing.assert_allclose(camera[:3, 3], 0, atol=1e-9)

    def test_without_vio_waits_for_accelerometer_gravity(self):
        with self.assertRaises(ValueError):
            self.pose.observe(None, np.eye(3), None, self.imu_from_rgb, 0.0, {})
        self.pose.gravity_up = np.array([0.0, -1.0, 0.0])
        self.pose.observe(None, np.eye(3), None, self.imu_from_rgb, 0.0, {})
        np.testing.assert_allclose(
            self.pose.reference[:3, :3] @ [0, -1, 0], [0, 0, 1], atol=1e-12
        )


if __name__ == "__main__":
    unittest.main()
