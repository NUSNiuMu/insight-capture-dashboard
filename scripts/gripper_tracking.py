#!/usr/bin/env python3

"""Live gripper open/close tracking from two ArUco stickers on the jaw tips.

The physical UMI-style gripper has one 4x4_50 ArUco marker per finger
(stock stickers; IDs are fixed by the manufacturer, not by us). We track the
pixel distance between the two marker centers on the same wrist camera frame
already used for display/alignment, and normalize it against calibrated
open/closed pixel-distance extremes captured live (see GripperCalibration).
Pixel space is used deliberately instead of a metric 3D pose: we only need a
0..1 knob to drive the avatar's finger nodes, not a physical measurement, and
staying in pixel space avoids depending on per-camera intrinsics being
available/accurate for this secondary feature.
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple

import cv2
import numpy as np

# Stock UMI gripper sticker IDs (cv2.aruco.DICT_4X4_50), confirmed against the
# physical rig: left finger = 1, right finger = 0.
LEFT_MARKER_ID = 1
RIGHT_MARKER_ID = 0

# If detection is lost for longer than this, fall back to reporting "unknown"
# (caller should hold the last displayed value) rather than snapping to a
# stale reading indefinitely.
DETECTION_HOLD_TIMEOUT_SEC = 2.0

DEFAULT_CALIBRATION_PATH = "config/gripper_calibration.json"


@dataclass
class GripperCalibration:
    open_px: Optional[float] = None
    closed_px: Optional[float] = None

    @property
    def is_valid(self) -> bool:
        return (
            self.open_px is not None
            and self.closed_px is not None
            and abs(self.open_px - self.closed_px) > 1e-3
        )

    def normalize(self, distance_px: float) -> Optional[float]:
        if not self.is_valid:
            return None
        opening = (distance_px - self.closed_px) / (self.open_px - self.closed_px)
        return float(min(1.0, max(0.0, opening)))


@dataclass
class GripperDetectionResult:
    distance_px: Optional[float]
    left_center_px: Optional[Tuple[float, float]]
    right_center_px: Optional[Tuple[float, float]]
    stamp_monotonic: float = field(default_factory=time.monotonic)

    @property
    def found_both(self) -> bool:
        return self.left_center_px is not None and self.right_center_px is not None


class GripperMarkerDetector:
    """Stateless-per-call ArUco detector for the two gripper finger markers."""

    def __init__(self) -> None:
        self._dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        self._params = cv2.aruco.DetectorParameters()
        self._detector = cv2.aruco.ArucoDetector(self._dictionary, self._params)

    def detect(self, image_bgr: np.ndarray) -> GripperDetectionResult:
        gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY) if image_bgr.ndim == 3 else image_bgr
        corners, ids, _ = self._detector.detectMarkers(gray)
        left_center = None
        right_center = None
        if ids is not None:
            for marker_corners, marker_id in zip(corners, ids.ravel()):
                center = marker_corners.reshape(4, 2).mean(axis=0)
                if int(marker_id) == LEFT_MARKER_ID:
                    left_center = (float(center[0]), float(center[1]))
                elif int(marker_id) == RIGHT_MARKER_ID:
                    right_center = (float(center[0]), float(center[1]))
        distance_px = None
        if left_center is not None and right_center is not None:
            distance_px = float(np.hypot(left_center[0] - right_center[0], left_center[1] - right_center[1]))
        return GripperDetectionResult(
            distance_px=distance_px,
            left_center_px=left_center,
            right_center_px=right_center,
        )


class GripperTrackingMixin:
    """Mixin providing live gripper-opening tracking, one instance per hand camera.

    Expected host attributes (matches the LiveAlignmentMixin convention used
    by PoseBridgeNode/DashboardNode): self.cameras (iterable with .name and
    .teleop_role), and a way to receive decoded BGR frames per camera name.
    """

    def _configure_gripper_tracking(self, calibration_path: str = DEFAULT_CALIBRATION_PATH) -> None:
        # teleop_role lives on PoseSpec, not CameraSpec — but pose.name == camera.name
        # (both built from the same camera_setup.build_dashboard_config entries), so
        # this set of names is directly usable to key camera-frame callbacks.
        self.gripper_tracking_cameras = {
            pose.name for pose in self.poses if getattr(pose, "teleop_role", None) in ("left_hand", "right_hand")
        }
        self.gripper_detector = GripperMarkerDetector() if self.gripper_tracking_cameras else None
        self.gripper_calibration_path = Path(calibration_path)
        self.gripper_calibrations: Dict[str, GripperCalibration] = {
            name: GripperCalibration() for name in self.gripper_tracking_cameras
        }
        self._load_gripper_calibration()
        self.gripper_latest_result: Dict[str, GripperDetectionResult] = {}
        self.gripper_last_opening: Dict[str, float] = {}

    def _load_gripper_calibration(self) -> None:
        if not self.gripper_calibration_path.is_file():
            return
        try:
            data = json.loads(self.gripper_calibration_path.read_text())
        except (OSError, ValueError):
            return
        for name, calib in self.gripper_calibrations.items():
            entry = data.get(name)
            if not entry:
                continue
            calib.open_px = entry.get("open_px")
            calib.closed_px = entry.get("closed_px")

    def _process_gripper_image(self, camera_name: str, image_bgr: np.ndarray) -> None:
        if camera_name not in self.gripper_tracking_cameras or self.gripper_detector is None:
            return
        result = self.gripper_detector.detect(image_bgr)
        self.gripper_latest_result[camera_name] = result
        if result.distance_px is None:
            return
        calib = self.gripper_calibrations[camera_name]
        opening = calib.normalize(result.distance_px)
        if opening is not None:
            self.gripper_last_opening[camera_name] = opening

    def gripper_opening_percent(self, camera_name: str) -> Optional[float]:
        """Returns 0 (closed) .. 1 (open), or None if never detected / not a hand camera.

        Holds the last known value across brief detection dropouts (occlusion,
        motion blur) rather than snapping back to "unknown", per the same
        reasoning as the live alignment status text: a momentarily-lost
        marker shouldn't visibly jerk the avatar.
        """
        result = self.gripper_latest_result.get(camera_name)
        if result is not None and (time.monotonic() - result.stamp_monotonic) > DETECTION_HOLD_TIMEOUT_SEC:
            return None
        return self.gripper_last_opening.get(camera_name)
