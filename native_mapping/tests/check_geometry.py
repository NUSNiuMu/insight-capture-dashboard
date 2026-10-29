"""Exercise real mapping, BA, cross-camera pose geometry, and reset without ROS."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile

import numpy as np
from scipy.spatial.transform import Rotation

from insight_capture.runtime.mapping.looper_backend import LooperBackend


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    rng = np.random.default_rng(73)
    points = rng.uniform([-.5, -.6, 1.2], [.5, .6, 2.0], (250, 3))
    descriptors = rng.uniform(size=(len(points), 256)).astype(np.float32)
    descriptors /= np.linalg.norm(descriptors, axis=1, keepdims=True)
    models = []
    for index in range(4):
        E = np.eye(4)
        E[0, 3] = .1 if index == 1 else 0
        models.append(dict(width=544, height=640, intrinsics=[306., 306., 272., 320.],
                           imu_from_camera=E.tolist()))
    image = np.zeros((640, 544), np.uint8)

    def pixels(pose, camera):
        T = pose @ np.asarray(models[camera]["imu_from_camera"])
        xyz = (points - T[:3, 3]) @ T[:3, :3]
        return (xyz[:, :2] / xyz[:, 2:] * 306 + [272, 320]).astype(np.float32)

    with tempfile.TemporaryDirectory() as directory:
        sock = Path(directory) / "mapping.sock"
        process = subprocess.Popen([str(args.binary), str(sock)], stdout=subprocess.PIPE, text=True)
        try:
            assert process.stdout.readline().strip() == "ready", "backend startup failed"
            client = LooperBackend(sock)
            client.request("initialize", cameras=models)
            empty = client.request("map", stamp_ns=500_000_000,
                                   odom_from_imu=np.eye(4).tolist(), images=[image, image],
                                   arrays=[np.empty((0, 2), np.float32), np.empty((0, 2), np.float32),
                                           np.empty((0, 256), np.float32)])
            assert not empty["mapped"] and empty["keyframes"] == 0, empty
            client.request("reset")
            for i in range(12):
                pose = np.eye(4)
                pose[0, 3] = i * .002
                result = client.request("map", stamp_ns=1_000_000_000 + i * 500_000_000,
                                        odom_from_imu=pose.tolist(), images=[image, image],
                                        arrays=[pixels(pose, 0), pixels(pose, 1), descriptors])
            assert result["landmarks"] >= 40, result
            geometry = client.request("geometry")["arrays"]
            assert geometry[0].shape[1] == 259 and len(geometry[0]) >= 40
            assert geometry[1].shape[1] == 5
            errors = {}
            for camera in (2, 3):
                truth = np.eye(4)
                truth[:3, :3] = Rotation.from_euler("xyz", [3, -5, 7], degrees=True).as_matrix()
                truth[:3, 3] = [.12 * (camera - 1), -.03, .02]
                odom = np.eye(4)
                odom[:3, 3] = [15, -8, 3]
                result = client.request("localize", stamp_ns=8_000_000_000, tracking_epoch=0,
                                        camera_id=camera, odom_from_imu=odom.tolist(), images=[image],
                                        arrays=[pixels(truth, camera), descriptors])
                assert result["localized"], result
                actual = np.asarray(result["map_from_imu"])
                position = np.linalg.norm(actual[:3, 3] - truth[:3, 3])
                angle = Rotation.from_matrix(truth[:3, :3].T @ actual[:3, :3]).magnitude()
                assert position < .0001 and angle < .0001, (position, angle)
                errors[camera] = dict(position_mm=position * 1000, angle_deg=np.degrees(angle))
            pose = np.eye(4)
            pose[0, 3] = .024
            updated = client.request("map", stamp_ns=7_000_000_000,
                                     odom_from_imu=pose.tolist(), images=[image, image],
                                     arrays=[pixels(pose, 0), pixels(pose, 1), descriptors])
            assert updated["map_revision"] == 13
            assert {r["camera_id"] for r in updated["wrist_updates"]} == {2, 3}
            assert all(r["localized"] and r["map_revision"] == 13
                       for r in updated["wrist_updates"]), updated
            bad_pixels = rng.uniform([0, 0], [544, 512], (len(points), 2)).astype(np.float32)
            rejected = client.request("localize", stamp_ns=8_500_000_000, camera_id=2,
                                      odom_from_imu=odom.tolist(), images=[image],
                                      arrays=[bad_pixels, descriptors])
            assert not rejected["localized"], rejected
            result = client.request("reset")
            assert result["keyframes"] == result["landmarks"] == result["raw_landmarks"] == 0
            assert result["map_revision"] == 0
            pose = np.eye(4)
            rebuilt = client.request("map", stamp_ns=1_000_000_000,
                                      odom_from_imu=pose.tolist(), images=[image, image],
                                      arrays=[pixels(pose, 0), pixels(pose, 1), descriptors])
            assert rebuilt["keyframes"] == 1 and rebuilt["raw_landmarks"] > 0, rebuilt
            assert rebuilt["map_revision"] == 1 and not rebuilt["wrist_updates"]
            client.close()
            print(json.dumps({"geometry": errors, "reset": "passed"}))
        finally:
            process.terminate()
            process.wait(timeout=5)


if __name__ == "__main__":
    main()
