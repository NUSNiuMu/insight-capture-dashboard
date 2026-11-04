import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from insight_capture.aruco_capture.frame_migration import convert, revert
from insight_capture.aruco_capture.pose import GRAVITY_FRAME


def _session(directory, vio_enabled=True):
    world_from_imu = np.eye(4)
    world_from_imu[:3, :3] = Rotation.from_euler("xyz", [100, 10, 40], degrees=True).as_matrix()
    world_from_imu[:3, 3] = [0.3, -0.2, 1.1]
    meta = {
        "status": "stopped",
        "config": {"vio_enabled": vio_enabled},
        "reference_from_vio_world": np.linalg.inv(world_from_imu).tolist(),
        "imu_from_rgb": np.eye(4).tolist(),
    }
    (directory / "session.json").write_text(json.dumps(meta))
    return world_from_imu


class FrameMigrationTest(unittest.TestCase):
    def test_convert_makes_vio_up_the_reference_z_and_reverts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            world_from_imu = _session(path)
            original = (path / "session.json").read_text()
            self.assertTrue(convert(path))
            self.assertFalse(convert(path))
            meta = json.loads((path / "session.json").read_text())
            self.assertEqual(meta["reference_frame"], GRAVITY_FRAME)
            reference = np.array(meta["reference_from_vio_world"])
            np.testing.assert_allclose(reference[:3, :3] @ [0, 0, 1], [0, 0, 1], atol=1e-12)
            # Live poses (legacy reference frame) map onto the new reference.
            legacy = np.array(meta["legacy_reference_from_vio_world"])
            np.testing.assert_allclose(
                np.array(meta["live_pose_correction"]) @ legacy, reference, atol=1e-12
            )
            np.testing.assert_allclose(
                np.array(meta["reference_from_rgb"]), reference @ world_from_imu, atol=1e-12
            )
            self.assertTrue(revert(path))
            self.assertEqual(json.loads((path / "session.json").read_text()), json.loads(original))

    def test_sessions_without_vio_cannot_be_converted(self):
        with tempfile.TemporaryDirectory() as tmp:
            _session(Path(tmp), vio_enabled=False)
            with self.assertRaises(ValueError):
                convert(Path(tmp))


if __name__ == "__main__":
    unittest.main()
