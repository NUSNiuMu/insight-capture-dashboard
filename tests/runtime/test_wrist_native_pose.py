"""Native poses remain unmodified; clock rollback still clears map localization."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
import numpy as np
from geometry_msgs.msg import PoseStamped
from insight_capture.runtime.mapping.adaptive_relocalization import AdaptiveRelocalizationConfig
from insight_capture.runtime.mapping.global_localization import GlobalLocalizationConfig
from insight_capture.runtime.mapping.insight3_localizer import CameraState, Insight3GlobalLocalizer
from insight_capture.runtime.mapping.relocalization_ekf import RelocalizationEkfConfig

class WristNativePoseTest(unittest.TestCase):

    def make_callback(self):
        state = CameraState('insight3_a', GlobalLocalizationConfig(), history_points=100, ekf_config=RelocalizationEkfConfig(), adaptive_config=AdaptiveRelocalizationConfig())
        state.pose_filter.reinitialize(np.eye(4))
        state.imu_to_center = np.eye(4)
        state.vio_tracking_status = 'TRACKING_STATIC'
        state.vio_tracking_status_monotonic = time.monotonic()
        publisher = Mock()
        node = SimpleNamespace(_cameras={'insight3_a': state}, _args=SimpleNamespace(pose_publish_hz=30.0, path_interval_ms=50.0), _pose_publishers={'insight3_a': publisher}, _map_frame='insight9_map')
        return (state, publisher, Insight3GlobalLocalizer._vio_callback(node, 'insight3_a'))

    def message(self, stamp_ns, x):
        msg = PoseStamped()
        msg.header.stamp.sec = stamp_ns // 10 ** 9
        msg.header.stamp.nanosec = stamp_ns % 10 ** 9
        msg.pose.position.x = float(x)
        msg.pose.orientation.w = 1.0
        return msg

    def test_static_status_never_rewrites_a_native_pose_jump(self):
        (state, publisher, callback) = self.make_callback()
        for (i, x) in enumerate([0.0, 0.001, 0.2, 0.201]):
            callback(self.message(10 ** 9 + i * 40000000, x))
        self.assertEqual(publisher.publish.call_count, 4)
        np.testing.assert_allclose([c.args[0].pose.position.x for c in publisher.publish.call_args_list], [0.0, 0.001, 0.2, 0.201])
        self.assertEqual(state.pose_buffer.lookup(1080000000).translation[0], 0.2)

    def test_native_clock_rollback_requires_new_visual_localization(self):
        (state, publisher, callback) = self.make_callback()
        callback(self.message(2000000000, 0.0))
        callback(self.message(1000000000, 0.1))
        self.assertFalse(state.pose_filter.initialized)
        self.assertEqual(publisher.publish.call_count, 1)
if __name__ == '__main__':
    unittest.main()
