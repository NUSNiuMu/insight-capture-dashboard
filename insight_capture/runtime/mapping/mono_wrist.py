"""Fisheye calibration shared by live and recorded UVC wrist queries."""

import json
from pathlib import Path

import cv2
import numpy as np

from insight_capture.runtime.camera_profiles import read_json


def load_intrinsics(raw):
    info = raw["camera_calibrations"][0]["intrinsics"]
    if info["camera_model"].upper() != "FISHEYE":
        raise ValueError("Expected a FISHEYE calibration")
    width, height = int(info["width"]), int(info["height"])
    fx, fy, cx, cy = (float(info[key]) for key in ("fx", "fy", "cx", "cy"))
    distortion = np.asarray(info["distortion_coef"], dtype=float).reshape(4)
    if min(width, height, fx, fy) <= 0 or not np.isfinite([fx, fy, cx, cy, *distortion]).all():
        raise ValueError("Invalid fisheye intrinsics")
    return width, height, [fx, 0., cx, 0., fy, cy, 0., 0., 1.], distortion.tolist()


def optical_rotation(degrees):
    if degrees not in (0, 180):
        raise ValueError("Wrist installation rotation must be 0 or 180 degrees")
    return np.diag([-1., -1., 1.] if degrees == 180 else [1., 1., 1.])


def wrist_rectification(k, d, rotation, size):
    f = min(size) / 2
    new_k = np.array([[f, 0, (size[0] - 1) / 2], [0, f, (size[1] - 1) / 2], [0, 0, 1]])
    optical = np.eye(4)
    optical[:3, :3] = rotation
    maps = cv2.fisheye.initUndistortRectifyMap(np.asarray(k).reshape(3, 3), np.asarray(d),
                                             rotation, new_k, size, cv2.CV_16SC2)
    model = {"width": size[0], "height": size[1], "intrinsics": [f, f, new_k[0, 2], new_k[1, 2]],
             "imu_from_camera": np.eye(4).tolist()}
    return model, maps, optical


def device_calibration(config: Path, device, devices):
    entries = read_json(config / "uvc-localization.json", [])
    if not isinstance(entries, list):
        raise ValueError("uvc-localization.json must contain a list of camera calibrations")
    matches = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("product") != device["product"]:
            continue
        field = "serial" if entry.get("serial") else "port"
        if not entry.get(field) or entry[field] != device[field]:
            continue
        if sum(item["product"] == device["product"] and item[field] == device[field]
               for item in devices) == 1:
            matches.append(entry)
    if len(matches) > 1:
        raise ValueError("Multiple calibrations match this UVC camera")
    if not matches:
        return None
    entry = matches[0]
    calibration = load_intrinsics(json.loads((config / entry["calibration"]).read_text()))
    return (*calibration, optical_rotation(entry.get("rotation_deg", 0)))
