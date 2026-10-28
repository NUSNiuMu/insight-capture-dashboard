"""Physical pose fusion must not depend on an arbitrary odom coordinate origin."""
import unittest
import cv2
import numpy as np
from insight_capture.runtime.mapping.relocalization_ekf import RelocalizationEkf, RelocalizationEkfConfig, correction_covariance
from insight_capture.runtime.mapping.adaptive_relocalization import AdaptiveRelocalizationConfig, AdaptiveRelocalizationPolicy

def pose(t, r=(0.0, 0.0, 0.0)):
    T = np.eye(4)
    T[:3, 3] = t
    T[:3, :3] = cv2.Rodrigues(np.array(r, float))[0]
    return T

class RelocalizationCovarianceTest(unittest.TestCase):

    def test_same_physical_update_is_invariant_to_odom_origin_and_orientation(self):
        N = pose((0.3, -0.4, 0.6), (0.1, -0.2, 0.3))
        C = pose((0.02, 0.01, -0.03), (0.01, 0.02, -0.01))
        M = pose((0.024, 0.008, -0.025), (0.015, 0.012, -0.005))
        H = pose((20.0, -15.0, 8.0), (0.2, 0.3, -0.1))
        outputs = []
        for (n, c, m) in [(N, C, M), (H @ N, C @ np.linalg.inv(H), M @ np.linalg.inv(H))]:
            f = RelocalizationEkf(RelocalizationEkfConfig())
            f.reinitialize(c)
            f.predict(0.2, n)
            f.observe(m, n, correction_covariance(m, n, 0.003, 0.2))
            outputs.append(f.correction @ n)
        np.testing.assert_allclose(outputs[0], outputs[1], atol=1e-10)

    def test_rotation_observation_cannot_move_an_unchanged_camera_position(self):
        N = pose((2.0, -1.0, 0.5))
        observed = N.copy()
        observed[:3, :3] = cv2.Rodrigues(np.array([0.0, 0.0, 0.03]))[0]
        C = observed @ np.linalg.inv(N)
        f = RelocalizationEkf(RelocalizationEkfConfig())
        f.reinitialize(np.eye(4))
        f.predict(0.2, N)
        f.observe(C, N, correction_covariance(C, N, 0.001, 0.05))
        np.testing.assert_allclose((f.correction @ N)[:3, 3], N[:3, 3], atol=1e-10)

    def test_prediction_preserves_real_native_camera_motion(self):
        f = RelocalizationEkf(RelocalizationEkfConfig())
        C = pose((0.1, 0.2, 0.3))
        f.reinitialize(C)
        N = pose((0.3, -0.1, 0.2), (0.05, 0.1, 0.02))
        f.predict(0.1, N)
        np.testing.assert_allclose(f.correction @ N, C @ N)

    def test_observation_uncertainty_controls_update_strength(self):
        N = np.eye(4)
        M = pose((0.02, 0.0, 0.0))
        results = []
        for sigma in [0.001, 0.1]:
            f = RelocalizationEkf(RelocalizationEkfConfig())
            f.reinitialize(np.eye(4))
            f.predict(0.1, N)
            f.observe(M, N, correction_covariance(M, N, sigma, 0.1))
            results.append(f.correction[0, 3])
        self.assertGreater(results[0], 0.018)
        self.assertLess(results[1], 0.001)

    def test_invalid_observation_covariance_does_not_initialize(self):
        f = RelocalizationEkf(RelocalizationEkfConfig())
        with self.assertRaises(np.linalg.LinAlgError):
            f.observe(np.eye(4), np.eye(4), np.zeros((6, 6)))
        self.assertFalse(f.initialized)

    def test_jump_policy_uses_physical_camera_displacement(self):
        N = pose((4.0, -1.0, 0.5))
        observed = N.copy()
        observed[:3, :3] = cv2.Rodrigues(np.array([0.0, 0.0, 0.05]))[0]
        C = observed @ np.linalg.inv(N)
        f = RelocalizationEkf(RelocalizationEkfConfig())
        f.reinitialize(np.eye(4))
        f.predict(0.2, N)
        policy = AdaptiveRelocalizationPolicy(AdaptiveRelocalizationConfig())
        result = policy.apply(f, C, N, correction_covariance(C, N, 0.001, 0.05))
        self.assertEqual(result.mode, 'ekf')
        self.assertLess(result.translation_m, 1e-12)

    def test_marker_style_covariance_supports_initialization_and_large_position_recovery(self):
        N = pose((0.2, -0.1, 0.3))
        f = RelocalizationEkf(RelocalizationEkfConfig())
        policy = AdaptiveRelocalizationPolicy(AdaptiveRelocalizationConfig())
        C = np.eye(4)
        result = policy.apply(f, C, N, correction_covariance(C, N, 0.01, 1.0))
        self.assertEqual(result.mode, 'initialize')
        C = pose((0.2, 0.0, 0.0))
        result = policy.apply(f, C, N, correction_covariance(C, N, 0.01, 1.0))
        self.assertEqual(result.mode, 'jump')
        np.testing.assert_allclose(f.correction, C)
if __name__ == '__main__':
    unittest.main()
