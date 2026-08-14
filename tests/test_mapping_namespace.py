import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from camera_setup import (  # noqa: E402
    sparse_mapping_camera_frame,
    sparse_mapping_map_frame,
    sparse_mapping_prefix,
    sparse_mapping_topic,
)
from post_processing_core.topic_catalog import (  # noqa: E402
    _topic_group,
    build_recording_topic_catalog,
    filter_recordable_live_topics,
)


class MappingNamespaceTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "cameras": [
                {
                    "name": "insight7_c",
                    "namespace": "insight7_c",
                    "teleop_role": "head",
                    "dashboard_pose_stream": (
                        "/insight_mapping/insight7_c/sparse_map/pose"
                    ),
                }
            ]
        }

    def test_head_mapping_names_are_camera_scoped(self):
        self.assertEqual(
            sparse_mapping_prefix("insight7_c"),
            "/insight_mapping/insight7_c/sparse_map",
        )
        self.assertEqual(
            sparse_mapping_topic("insight7_c", "status"),
            "/insight_mapping/insight7_c/sparse_map/status",
        )
        self.assertEqual(sparse_mapping_map_frame("insight7_c"), "insight7_c_map")
        self.assertEqual(
            sparse_mapping_camera_frame("insight7_c"),
            "insight7_c_mapping_camera_center",
        )

    def test_scoped_mapping_topics_remain_recordable_and_grouped(self):
        pose_topic = "/insight_mapping/insight7_c/sparse_map/pose"
        filtered = filter_recordable_live_topics(
            self.config,
            [
                pose_topic,
                "/insight9_sparse_map/status",
                "/insight_global/another_rig/status",
            ],
        )
        self.assertEqual(filtered, [pose_topic])
        catalog = build_recording_topic_catalog(self.config, filtered)
        self.assertEqual(catalog["cameras"][0]["topics"][0]["name"], pose_topic)
        self.assertEqual(_topic_group(pose_topic, self.config), "insight7_c")


if __name__ == "__main__":
    unittest.main()
