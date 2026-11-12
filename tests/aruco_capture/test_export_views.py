import tempfile
import unittest
from pathlib import Path

import numpy as np

from insight_capture.aruco_capture import export_views, segment_settings


class SegmentSettingsTests(unittest.TestCase):
    def test_defaults_save_reset(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(segment_settings.load(root), segment_settings.defaults())
            saved = segment_settings.save(root, {"idle_duration_s": 1.5, "unknown": 3})
            self.assertEqual(saved["idle_duration_s"], 1.5)
            self.assertEqual(segment_settings.load(root)["idle_duration_s"], 1.5)
            with self.assertRaises(ValueError):
                segment_settings.save(root, {"idle_duration_s": 99})
            self.assertEqual(segment_settings.save(root, {"reset": True}), segment_settings.defaults())
            self.assertEqual(len(segment_settings.describe(root)["fields"]), len(segment_settings.FIELDS))


class ExportViewsTests(unittest.TestCase):
    names = ["head", "rgb_left_up", "rgb_right_up"]

    def test_catalog_and_defaults(self):
        ids = {v["id"] for v in export_views.catalog(self.names[1:])}
        self.assertTrue({"head_pad448", "subtask_end_head", "hand_crop_left", "rgb_crop224:rgb_left_up"} <= ids)
        self.assertEqual(export_views.default_selection(self.names)[0], {"id": "head_raw", "key": "head"})

    def test_selection_validation(self):
        ok = export_views.validate_selection([{"id": "head_pad448", "key": "my_head"}], self.names)
        self.assertEqual(ok, [{"id": "head_pad448", "key": "my_head"}])
        for bad in (
            [],
            [{"id": "head_pad448", "key": "bad key"}],
            [{"id": "head_pad448", "key": "a"}, {"id": "head_pad224", "key": "a"}],
            [{"id": "rgb_crop224:rgb_left_down", "key": "x"}],
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                export_views.validate_selection(bad, self.names)

    def test_pad_and_crop_shapes(self):
        head = np.full((1088, 1920, 3), 200, np.uint8)
        padded = export_views.pad_square(head, 448)
        self.assertEqual(padded.shape, (448, 448, 3))
        self.assertEqual(int(padded[0, 224, 0]), 0)  # black bar on top
        self.assertEqual(int(padded[224, 224, 0]), 200)
        self.assertEqual(export_views.center_crop(np.zeros((720, 1280, 3), np.uint8), 224).shape, (224, 224, 3))


if __name__ == "__main__":
    unittest.main()
