"""Check camera-parameter uncertainty propagation into map-to-odom coordinates."""
import unittest
import cv2
import numpy as np
from insight_capture.runtime.mapping.global_localization import _map_odom_pose_covariance

class PoseMeasurementUncertaintyTest(unittest.TestCase):

    def test_analytic_propagation_matches_independent_finite_differences(self):
        rng = np.random.default_rng(41)
        objects = rng.uniform(-0.5, 0.5, (80, 3))
        objects[:, 2] += 2.0
        K = np.array([[350.0, 0, 270], [0, 350.0, 320], [0, 0, 1.0]])
        parameters = np.array([0.2, -0.1, 0.3, 0.1, 0.3, 0.8])
        (_, J) = cv2.projectPoints(objects, parameters[:3], parameters[3:], K, None)
        O = np.eye(4)
        O[:3, :3] = cv2.Rodrigues(np.array([-0.1, 0.4, 0.2]))[0]
        O[:3, 3] = [0.8, -1.2, 0.4]
        residuals = np.sin(np.arange(160)) * 0.8
        actual = _map_odom_pose_covariance(parameters[:3], parameters[3:], J, residuals, O)

        def correction(p):
            T = np.eye(4)
            T[:3, :3] = cv2.Rodrigues(p[:3])[0]
            T[:3, 3] = p[3:]
            return np.linalg.inv(T) @ np.linalg.inv(O)
        base = correction(parameters)

        def state(p):
            C = correction(p)
            d = base[:3, :3].T @ C[:3, :3]
            return np.r_[C[:3, 3], 0.5 * np.array([d[2, 1] - d[1, 2], d[0, 2] - d[2, 0], d[1, 0] - d[0, 1]])]
        numeric = np.column_stack([(state(parameters + np.eye(6)[i] * 1e-06) - state(parameters - np.eye(6)[i] * 1e-06)) / 2e-06 for i in range(6)])
        variance = max(0.25, float(np.sum(residuals ** 2)) / (160 - 6))
        expected = numeric @ np.linalg.inv(J[:, :6].T @ J[:, :6]) * variance @ numeric.T
        np.testing.assert_allclose(actual, expected, rtol=2e-06, atol=1e-12)
        self.assertGreater(np.linalg.eigvalsh(actual).min(), 0.0)

    def test_singular_geometry_is_rejected_without_a_replacement_covariance(self):
        with self.assertRaises(np.linalg.LinAlgError):
            _map_odom_pose_covariance(np.zeros(3), np.zeros(3), np.zeros((20, 6)), np.zeros(20), np.eye(4))
if __name__ == '__main__':
    unittest.main()
