"""Current visual observations must reach live localization independently of graph scheduling."""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import numpy as np
from insight_capture.runtime.mapping.global_localization import GlobalLocalizationConfig, LocalizationCandidate, LocalizationConsensus
from insight_capture.runtime.mapping.insight9_mapper import Insight9SparseMapper
from insight_capture.runtime.mapping.relocalization_ekf import RelocalizationEkf, RelocalizationEkfConfig

class HeadVisualCorrectionTest(unittest.TestCase):

    def check_visual_update(self, optimize_due):
        visual = np.eye(4)
        visual[0, 3] = 0.001
        graph_pose = np.eye(4)
        graph_pose[0, 3] = 0.02
        candidate = LocalizationCandidate(visual, visual, 100, 90, 0.9, 0.2, 8)
        pose_filter = RelocalizationEkf(RelocalizationEkfConfig())
        pose_filter.reinitialize(np.eye(4))
        pose_filter.predict(1.0, np.eye(4))
        graph = SimpleNamespace(pose=lambda _: graph_pose, correction_for_keyframe=lambda _: graph_pose, full=False, add_global_loop_edge=Mock())
        node = SimpleNamespace(_loop_closure_count=0, _capture_reference_keyframe=None, _capture_reference_points=None, _capture_reference_descriptors=None, _map_lock=threading.Lock(), _loop_lock=threading.Lock(), _landmarks=SimpleNamespace(descriptors=lambda **_: (np.ones((100, 3)), np.ones((100, 256)))), _loop_config=GlobalLocalizationConfig(confirmation_frames=1), _loop_consensus=LocalizationConsensus(GlobalLocalizationConfig(confirmation_frames=1)), _last_loop_measurement=None, _loop_pose_filter=pose_filter, _pose_graph=graph, _pose_graph_pending=False, _pose_graph_optimization_count=1, _last_pose_graph_optimization_monotonic=float('-inf') if optimize_due else time.monotonic(), _optimize_pose_graph=Mock(return_value=SimpleNamespace(optimized=True)), _args=SimpleNamespace(loop_closure_enabled=True, loop_check_interval_keyframes=1, loop_min_map_features=80, pose_graph_min_interval_sec=5.0, pose_graph_force_translation_m=0.1, pose_graph_force_rotation_deg=5.0), get_logger=lambda : SimpleNamespace(info=lambda *_: None))
        with patch('insight_capture.runtime.mapping.insight9_mapper.localize_features', return_value=(candidate, {'accepted': True, 'pose_covariance': (np.eye(6) * 1e-06).tolist()})):
            (diagnostics, optimized) = Insight9SparseMapper._detect_loop_closure(node, 40, 1000, np.zeros((100, 2)), np.ones((100, 256)), SimpleNamespace(left_projection=np.eye(3, 4)), np.eye(4), (640, 544), graph_target_id=40)
        self.assertEqual(optimized, optimize_due)
        self.assertEqual(node._optimize_pose_graph.call_count, int(optimize_due))
        np.testing.assert_allclose(pose_filter.correction[:3, 3], visual[:3, 3], atol=0.0001)
        graph.add_global_loop_edge.assert_called_once()
        np.testing.assert_array_equal(graph.add_global_loop_edge.call_args.args[1], visual)

    def test_pending_graph_does_not_delay_current_visual_correction(self):
        self.check_visual_update(False)

    def test_optimized_graph_does_not_replace_current_visual_correction(self):
        self.check_visual_update(True)
if __name__ == '__main__':
    unittest.main()
