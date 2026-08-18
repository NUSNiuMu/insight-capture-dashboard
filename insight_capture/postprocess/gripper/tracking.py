"""Track normalized gripper opening from two calibrated ArUco markers."""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple

import cv2
import numpy as np

from insight_capture.common.performance import track

# Stock UMI gripper sticker IDs (cv2.aruco.DICT_4X4_50), confirmed against the
# physical rig: left finger = 1, right finger = 0.
LEFT_MARKER_ID = 1
RIGHT_MARKER_ID = 0

# If detection is lost for longer than this, fall back to reporting "unknown"
# (caller should hold the last displayed value) rather than snapping to a
# stale reading indefinitely.
DETECTION_HOLD_TIMEOUT_SEC = 2.0

# Resolve calibration relative to this module, not the caller's CWD.
DEFAULT_CALIBRATION_PATH = str(
    Path(__file__).resolve().parents[3] / "config" / "gripper_calibration.json"
)


@dataclass
class GripperCalibration:
    open_px: Optional[float] = None
    closed_px: Optional[float] = None
    width_calibration: Tuple[Tuple[float, float], ...] = ()

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

    @property
    def has_metric_width(self) -> bool:
        if len(self.width_calibration) < 2:
            return False
        points = np.asarray(self.width_calibration, dtype=np.float64)
        if points.shape != (len(self.width_calibration), 2):
            return False
        width_deltas = np.diff(points[:, 1])
        return bool(
            np.all(np.isfinite(points))
            and np.all(np.diff(points[:, 0]) > 1e-3)
            and np.all(points[:, 1] >= 0.0)
            and (np.all(width_deltas > 0.0) or np.all(width_deltas < 0.0))
        )

    def width_m(self, distance_px: float) -> Optional[float]:
        """Map marker distance to measured jaw width using calibration points."""
        if not self.has_metric_width:
            return None
        points = np.asarray(self.width_calibration, dtype=np.float64)
        return float(
            np.interp(
                float(distance_px),
                points[:, 0],
                points[:, 1],
                left=points[0, 1],
                right=points[-1, 1],
            )
        )


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
    """Provide live gripper-opening state for configured hand cameras."""

    def _configure_gripper_tracking(self, calibration_path: str = DEFAULT_CALIBRATION_PATH) -> None:
        # Pose and camera names share the same configuration keys.
        hand_camera_names = {
            pose.name for pose in self.poses if getattr(pose, "teleop_role", None) in ("left_hand", "right_hand")
        }
        # Calibration keys are capabilities; tracking membership is the live toggle.
        self.gripper_tracking_cameras: set = set()
        self.gripper_detector = None
        self.gripper_calibration_path = Path(calibration_path)
        self.gripper_calibrations: Dict[str, GripperCalibration] = {
            name: GripperCalibration() for name in hand_camera_names
        }
        self._load_gripper_calibration()
        if any(calibration.is_valid for calibration in self.gripper_calibrations.values()):
            self.gripper_detector = GripperMarkerDetector()
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
            try:
                calib.width_calibration = tuple(
                    (float(point["distance_px"]), float(point["width_m"]))
                    for point in entry.get("width_calibration", [])
                )
            except (KeyError, TypeError, ValueError):
                calib.width_calibration = ()

    def _process_gripper_image(self, camera_name: str, image_bgr: np.ndarray) -> None:
        if camera_name not in self.gripper_tracking_cameras or self.gripper_detector is None:
            return
        with track(f"gripper_detect:{camera_name}"):
            result = self.gripper_detector.detect(image_bgr)
        self.gripper_latest_result[camera_name] = result
        if result.distance_px is None:
            return
        calib = self.gripper_calibrations[camera_name]
        opening = calib.normalize(result.distance_px)
        if opening is not None:
            self.gripper_last_opening[camera_name] = opening

    def set_gripper_tracking_enabled(self, camera_name: str, enabled: bool) -> None:
        # Calibration keys define which cameras support tracking.
        if camera_name not in self.gripper_calibrations:
            raise ValueError(f"'{camera_name}' is not a hand camera with gripper tracking configured")
        if not self.gripper_calibrations[camera_name].is_valid:
            raise ValueError(f"'{camera_name}' does not have a valid gripper calibration")
        if enabled:
            self.gripper_tracking_cameras.add(camera_name)
        else:
            self.gripper_tracking_cameras.discard(camera_name)

    def gripper_opening_percent(self, camera_name: str) -> Optional[float]:
        """Return 0..1 opening, holding brief detection dropouts."""
        result = self.gripper_latest_result.get(camera_name)
        if result is not None and (time.monotonic() - result.stamp_monotonic) > DETECTION_HOLD_TIMEOUT_SEC:
            return None
        return self.gripper_last_opening.get(camera_name)
