import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

from insight_capture.runtime.mapping.cube_markers import (
    MultiCubeMarkerEstimator,
    grayscale_marker_image,
    load_cube_marker_config,
    marker_map_to_odom,
)
from insight_capture.runtime.mapping.geometry import rotation_distance_deg


PROJECT_ROOT = Path(__file__).resolve().parents[2]
JETSON_RUNTIME_CONFIG = PROJECT_ROOT / "config/devices/jetson-nx/runtime.json"


FRONT_CORNERS = [
    [-0.04, -0.04, 0.00],
    [0.04, -0.04, 0.00],
    [0.04, 0.04, 0.00],
    [-0.04, 0.04, 0.00],
]
RIGHT_CORNERS = [
    [0.05, -0.04, 0.00],
    [0.05, -0.04, 0.08],
    [0.05, 0.04, 0.08],
    [0.05, 0.04, 0.00],
]


def _runtime_payload(*, duplicate_id=False):
    return {
        "cube_marker_relative_localization": {
            "enabled": True,
            "apply_corrections": False,
            "min_markers": 1,
            "max_reprojection_error_px": 1.0,
            "targets": [
                {
                    "camera": "insight3_a",
                    "cube_from_camera_center": {
                        "translation_m": [0.0, 0.0, 0.0],
                        "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
                    },
                    "markers": [
                        {"id": 10, "corners_cube_m": FRONT_CORNERS},
                        {"id": 11, "corners_cube_m": RIGHT_CORNERS},
                    ],
                },
                *(
                    [
                        {
                            "camera": "insight3_b",
                            "cube_from_camera_center": {
                                "translation_m": [0.0, 0.0, 0.0],
                                "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
                            },
                            "markers": [
                                {"id": 10, "corners_cube_m": FRONT_CORNERS}
                            ],
                        }
                    ]
                    if duplicate_id
                    else []
                ),
            ],
        }
    }


class CubeMarkerTests(unittest.TestCase):
    def _load(self, payload):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return load_cube_marker_config(path)

    def test_missing_runtime_file_disables_feature(self):
        config = load_cube_marker_config(Path("/does/not/exist/runtime.json"))
        self.assertFalse(config.enabled)
        self.assertEqual(config.targets, {})

    def test_duplicate_marker_ids_across_cubes_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self._load(_runtime_payload(duplicate_id=True))

    def test_corrections_default_to_shadow_mode(self):
        payload = _runtime_payload()
        del payload["cube_marker_relative_localization"]["apply_corrections"]
        self.assertFalse(self._load(payload).apply_corrections)

    def test_compressed_marker_image_decodes_to_grayscale(self):
        source = np.zeros((24, 32, 3), dtype=np.uint8)
        source[:, 16:] = (0, 128, 255)
        ok, encoded = cv2.imencode(".jpg", source)
        self.assertTrue(ok)
        decoded = grayscale_marker_image(
            SimpleNamespace(format="jpeg", data=encoded.tobytes())
        )
        self.assertEqual(decoded.shape, (24, 32))
        self.assertEqual(decoded.dtype, np.uint8)

    def test_jetson_profile_uses_official_umi_cube_geometry_with_corrections(self):
        config = load_cube_marker_config(JETSON_RUNTIME_CONFIG)
        self.assertTrue(config.enabled)
        self.assertTrue(config.apply_corrections)
        self.assertEqual(set(config.targets), {"insight3_a", "insight3_b"})

        expected_geometry = {
            8: [[-0.03, -0.035, 0.03], [0.03, -0.035, 0.03], [0.03, -0.035, -0.03], [-0.03, -0.035, -0.03]],
            9: [[-0.03, -0.03, -0.035], [0.03, -0.03, -0.035], [0.03, 0.03, -0.035], [-0.03, 0.03, -0.035]],
            10: [[-0.035, -0.03, 0.03], [-0.035, -0.03, -0.03], [-0.035, 0.03, -0.03], [-0.035, 0.03, 0.03]],
            11: [[0.035, -0.03, -0.03], [0.035, -0.03, 0.03], [0.035, 0.03, 0.03], [0.035, 0.03, -0.03]],
        }
        expected_marker_ids = {
            "insight3_a": (8, 9, 10, 11),
            "insight3_b": (2, 3, 4, 5),
        }
        expected_translations = {
            "insight3_a": [0.0, -0.058, 0.09],
            "insight3_b": [0.0, -0.053, 0.093],
        }
        for camera, target in config.targets.items():
            marker_ids = expected_marker_ids[camera]
            self.assertEqual(set(target.marker_corners_cube_m), set(marker_ids))
            np.testing.assert_allclose(
                target.cube_from_camera_center[:3, 3],
                expected_translations[camera],
            )
            np.testing.assert_allclose(
                target.cube_from_camera_center[:3, :3], np.eye(3)
            )
            for marker_id, corners in target.marker_corners_cube_m.items():
                geometry_id = 8 + marker_ids.index(marker_id)
                np.testing.assert_allclose(corners, expected_geometry[geometry_id])
                edge_lengths = np.linalg.norm(
                    np.roll(corners, -1, axis=0) - corners,
                    axis=1,
                )
                np.testing.assert_allclose(edge_lengths, 0.06)
                self.assertTrue(np.all(np.abs(corners) <= 0.035))
                fixed_axes = np.ptp(corners, axis=0) < 1e-12
                self.assertEqual(int(np.count_nonzero(fixed_axes)), 1)
                self.assertAlmostEqual(
                    abs(float(corners[0, np.flatnonzero(fixed_axes)[0]])),
                    0.035,
                )

        estimator = MultiCubeMarkerEstimator(config)
        intrinsic = np.array(
            [[620.0, 0.0, 640.0], [0.0, 620.0, 360.0], [0.0, 0.0, 1.0]]
        )
        rotation_vector = np.array([0.12, -0.20, 0.05], dtype=np.float64)
        translation = np.array([0.04, -0.02, 0.70], dtype=np.float64)
        detections = {}
        for marker_id in (8, 9, 10):
            projected, _ = cv2.projectPoints(
                config.targets["insight3_a"].marker_corners_cube_m[marker_id],
                rotation_vector,
                translation,
                intrinsic,
                None,
            )
            detections[marker_id] = projected.reshape(4, 2)

        result = estimator.estimate(detections, intrinsic)["insight3_a"]
        expected_pose = np.eye(4)
        expected_pose[:3, :3], _ = cv2.Rodrigues(rotation_vector)
        expected_pose[:3, 3] = translation
        self.assertEqual(result.marker_ids, (8, 9, 10))
        self.assertLess(
            np.linalg.norm(result.rgb_from_cube[:3, 3] - translation), 1e-5
        )
        self.assertLess(
            rotation_distance_deg(result.rgb_from_cube, expected_pose), 1e-4
        )

    def test_two_visible_faces_recover_one_rigid_cube_pose(self):
        config = self._load(_runtime_payload())
        estimator = MultiCubeMarkerEstimator(config)
        intrinsic = np.array(
            [[620.0, 0.0, 640.0], [0.0, 620.0, 360.0], [0.0, 0.0, 1.0]]
        )
        rotation_vector = np.array([0.10, -0.18, 0.06], dtype=np.float64)
        translation = np.array([0.07, -0.03, 0.75], dtype=np.float64)
        detections = {}
        for marker_id, corners in ((10, FRONT_CORNERS), (11, RIGHT_CORNERS)):
            projected, _ = cv2.projectPoints(
                np.asarray(corners, dtype=np.float64),
                rotation_vector,
                translation,
                intrinsic,
                None,
            )
            detections[marker_id] = projected.reshape(4, 2)

        result = estimator.estimate(detections, intrinsic)["insight3_a"]
        expected = np.eye(4)
        expected[:3, :3], _ = cv2.Rodrigues(rotation_vector)
        expected[:3, 3] = translation
        self.assertEqual(result.marker_ids, (10, 11))
        self.assertEqual(result.corners, 8)
        self.assertLess(np.linalg.norm(result.rgb_from_cube[:3, 3] - translation), 1e-5)
        self.assertLess(rotation_distance_deg(result.rgb_from_cube, expected), 1e-4)
        self.assertLess(result.max_reprojection_error_px, 1e-3)

    def test_marker_chain_produces_map_to_odom_correction(self):
        map_from_head = np.eye(4)
        map_from_head[:3, 3] = [1.0, 2.0, 3.0]
        head_from_rgb = np.eye(4)
        head_from_rgb[:3, 3] = [0.1, 0.0, 0.0]
        rgb_from_cube = np.eye(4)
        rgb_from_cube[:3, 3] = [0.0, 0.2, 0.8]
        cube_from_camera = np.eye(4)
        cube_from_camera[:3, 3] = [0.0, 0.0, -0.1]
        odom_from_camera = np.eye(4)
        odom_from_camera[:3, 3] = [0.2, 0.3, 0.4]

        map_from_camera, map_from_odom = marker_map_to_odom(
            map_from_head,
            head_from_rgb,
            rgb_from_cube,
            cube_from_camera,
            odom_from_camera,
        )
        np.testing.assert_allclose(map_from_camera[:3, 3], [1.1, 2.2, 3.7])
        np.testing.assert_allclose(map_from_odom[:3, 3], [0.9, 1.9, 3.3])


if __name__ == "__main__":
    unittest.main()
