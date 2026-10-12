"""Rectify a calibrated raw fisheye stereo pair for the pinhole mapper."""

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class FisheyeRectification:
    rotations: tuple
    projections: tuple
    maps: tuple
    size: tuple
    source_size: tuple = None

    @classmethod
    def create(cls, left_info, right_info, right_from_left, size=(544, 640), horizontal_fov_deg=90.0):
        if left_info.distortion_model != "equidistant" or right_info.distortion_model != "equidistant":
            raise ValueError("Fisheye rectification requires equidistant calibration")
        if (left_info.width, left_info.height) != (right_info.width, right_info.height):
            raise ValueError("Stereo calibration dimensions differ")
        size = tuple(int(v) for v in size)
        if min(size) <= 0:
            raise ValueError("Rectified dimensions must be positive")
        if not 30.0 <= horizontal_fov_deg <= 140.0:
            raise ValueError("Rectified horizontal FOV must be between 30 and 140 degrees")
        intrinsics = [np.asarray(info.k, dtype=np.float64).reshape(3, 3) for info in (left_info, right_info)]
        distortions = [np.asarray(info.d, dtype=np.float64).reshape(4, 1) for info in (left_info, right_info)]
        transform = np.asarray(right_from_left, dtype=np.float64).reshape(4, 4)
        if not all(np.isfinite(a).all() for a in [*intrinsics, *distortions, transform]):
            raise ValueError("Calibration must be finite")
        if np.linalg.norm(transform[:3, 3]) < 1e-4:
            raise ValueError("Stereo baseline is zero")
        r1, r2, p1, p2, _ = cv2.fisheye.stereoRectify(
            intrinsics[0], distortions[0], intrinsics[1], distortions[1],
            (left_info.width, left_info.height), transform[:3, :3], transform[:3, 3],
            flags=cv2.CALIB_ZERO_DISPARITY, newImageSize=size, balance=0.0, fov_scale=1.0,
        )
        if abs(p2[0, 3]) < 1e-6 or abs(p2[1, 3]) > 1e-6:
            raise ValueError("Mapper requires horizontal stereo rectification")
        # The portrait sensor includes pixels outside the usable fisheye circle.
        # A centered virtual pinhole avoids extrapolating those border rays.
        focal = size[0] / (2 * np.tan(np.deg2rad(horizontal_fov_deg) / 2))
        k = np.array([[focal, 0, (size[0]-1)/2], [0, focal, (size[1]-1)/2], [0, 0, 1]])
        p1 = np.column_stack((k, np.zeros(3)))
        p2 = p1.copy()
        rectified_translation = r2 @ transform[:3, 3]
        p2[:, 3] = k @ rectified_translation
        if p2[0, 3] >= 0:
            raise ValueError("Stereo eye ordering must produce positive left-to-right disparity")
        maps = tuple(cv2.fisheye.initUndistortRectifyMap(k, d, r, p, size, cv2.CV_32FC1)
                     for k, d, r, p in zip(intrinsics, distortions, (r1, r2), (p1, p2)))
        return cls((r1, r2), (p1, p2), maps, size, (left_info.width, left_info.height))

    def rectify(self, index, frame):
        if self.source_size is not None and frame.shape[:2] != self.source_size[::-1]:
            raise ValueError("Decoded image dimensions differ from rig calibration")
        return cv2.remap(frame, *self.maps[index], interpolation=cv2.INTER_LINEAR)
