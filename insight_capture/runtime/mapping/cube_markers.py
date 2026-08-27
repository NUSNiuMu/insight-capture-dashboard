"""Multi-face fiducial cube pose estimation for relative camera localization.

Each configured marker stores its four corners directly in the cube frame, in
the same top-left, top-right, bottom-right, bottom-left order returned by
OpenCV ArUco.  This avoids hiding a face-axis convention in runtime code and
lets the physical CAD or metrology result remain the source of truth.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

import cv2
import numpy as np

from .geometry import matrix_from_transform


@dataclass(frozen=True)
class CubeMarkerTarget:
    """One rigid marker cube attached to an Insight3 camera assembly."""

    camera: str
    cube_from_camera_center: np.ndarray
    marker_corners_cube_m: Mapping[int, np.ndarray]


@dataclass(frozen=True)
class CubeMarkerConfig:
    """Detection, geometry, synchronization and quality settings."""

    enabled: bool
    apply_corrections: bool
    dictionary_name: str
    image_topic: str
    camera_info_topic: str
    head_pose_topic: str
    head_left_frame: str
    head_right_frame: str
    head_rgb_frame: str
    detection_hz: float
    pose_wait_ms: float
    min_markers: int
    min_inlier_ratio: float
    max_reprojection_error_px: float
    min_depth_m: float
    max_depth_m: float
    confirmation_frames: int
    confirmation_window: int
    confirmation_translation_m: float
    confirmation_rotation_deg: float
    measurement_translation_std_m: float
    measurement_rotation_std_deg: float
    targets: Mapping[str, CubeMarkerTarget]


@dataclass(frozen=True)
class CubePoseEstimate:
    """A geometrically verified ``T_rgb_cube`` observation."""

    camera: str
    rgb_from_cube: np.ndarray
    marker_ids: tuple[int, ...]
    corners: int
    inliers: int
    inlier_ratio: float
    median_reprojection_error_px: float
    max_reprojection_error_px: float


def _positive_finite(value: object, name: str) -> float:
    result = float(value)
    if not np.isfinite(result) or result <= 0.0:
        raise ValueError(f"{name} must be positive")
    return result


def _transform_from_config(value: object, name: str) -> np.ndarray:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    try:
        translation = value["translation_m"]
        rotation = value["rotation_xyzw"]
    except KeyError as exc:
        raise ValueError(
            f"{name} requires translation_m and rotation_xyzw"
        ) from exc
    return matrix_from_transform(translation, rotation)


def load_cube_marker_config(path: Path) -> CubeMarkerConfig:
    """Load the optional ``cube_marker_relative_localization`` runtime section."""

    config_path = Path(path)
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        payload = {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"failed to read cube marker config: {exc}") from exc
    section = payload.get("cube_marker_relative_localization", {})
    if not isinstance(section, Mapping):
        raise ValueError("cube_marker_relative_localization must be an object")
    enabled = bool(section.get("enabled", False))

    targets: dict[str, CubeMarkerTarget] = {}
    marker_owners: dict[int, str] = {}
    raw_targets = section.get("targets", [])
    if not isinstance(raw_targets, list):
        raise ValueError("cube marker targets must be a list")
    for target_payload in raw_targets:
        if not isinstance(target_payload, Mapping):
            raise ValueError("each cube marker target must be an object")
        camera = str(target_payload.get("camera", "")).strip()
        if not camera:
            raise ValueError("cube marker target camera is required")
        if camera in targets:
            raise ValueError(f"duplicate cube marker target camera: {camera}")
        cube_from_camera_center = _transform_from_config(
            target_payload.get("cube_from_camera_center"),
            f"{camera}.cube_from_camera_center",
        )
        marker_corners: dict[int, np.ndarray] = {}
        raw_markers = target_payload.get("markers", [])
        if not isinstance(raw_markers, list) or not raw_markers:
            raise ValueError(f"{camera} requires at least one marker")
        for marker_payload in raw_markers:
            if not isinstance(marker_payload, Mapping):
                raise ValueError(f"{camera} marker must be an object")
            marker_id = int(marker_payload.get("id", -1))
            if marker_id < 0:
                raise ValueError(f"{camera} marker id must be non-negative")
            owner = marker_owners.get(marker_id)
            if owner is not None:
                raise ValueError(
                    f"marker id {marker_id} is ambiguous between {owner} and {camera}"
                )
            corners = np.asarray(
                marker_payload.get("corners_cube_m"), dtype=np.float64
            )
            if corners.shape != (4, 3) or not np.all(np.isfinite(corners)):
                raise ValueError(
                    f"{camera} marker {marker_id} corners_cube_m must be finite 4x3"
                )
            edge_lengths = np.linalg.norm(np.roll(corners, -1, axis=0) - corners, axis=1)
            if float(np.min(edge_lengths)) <= 1e-4:
                raise ValueError(f"{camera} marker {marker_id} has degenerate corners")
            marker_corners[marker_id] = corners
            marker_owners[marker_id] = camera
        targets[camera] = CubeMarkerTarget(
            camera=camera,
            cube_from_camera_center=cube_from_camera_center,
            marker_corners_cube_m=marker_corners,
        )

    if enabled and not targets:
        raise ValueError("enabled cube marker localization requires targets")

    confirmation_frames = int(section.get("confirmation_frames", 3))
    confirmation_window = int(section.get("confirmation_window", 5))
    if confirmation_frames <= 0 or confirmation_window < confirmation_frames:
        raise ValueError("invalid cube marker confirmation window")
    min_markers = int(section.get("min_markers", 1))
    if min_markers <= 0:
        raise ValueError("cube marker min_markers must be positive")
    min_inlier_ratio = float(section.get("min_inlier_ratio", 0.75))
    if not 0.0 < min_inlier_ratio <= 1.0:
        raise ValueError("cube marker min_inlier_ratio must be in (0, 1]")

    return CubeMarkerConfig(
        enabled=enabled,
        apply_corrections=bool(section.get("apply_corrections", False)),
        dictionary_name=str(section.get("dictionary", "DICT_4X4_50")),
        image_topic=str(
            section.get(
                "image_topic",
                "/insight9_a/camera/color/image_rect_raw/compressed",
            )
        ),
        camera_info_topic=str(
            section.get(
                "camera_info_topic", "/insight9_a/camera/color/camera_info"
            )
        ),
        head_pose_topic=str(
            section.get("head_pose_topic", "/insight9_sparse_map/pose")
        ),
        head_left_frame=str(
            section.get("head_left_frame", "insight9_a_camera_left")
        ),
        head_right_frame=str(
            section.get("head_right_frame", "insight9_a_camera_right")
        ),
        head_rgb_frame=str(
            section.get("head_rgb_frame", "insight9_a_camera_rgb")
        ),
        detection_hz=_positive_finite(section.get("detection_hz", 10.0), "detection_hz"),
        pose_wait_ms=_positive_finite(section.get("pose_wait_ms", 50.0), "pose_wait_ms"),
        min_markers=min_markers,
        min_inlier_ratio=min_inlier_ratio,
        max_reprojection_error_px=_positive_finite(
            section.get("max_reprojection_error_px", 2.5),
            "max_reprojection_error_px",
        ),
        min_depth_m=_positive_finite(section.get("min_depth_m", 0.10), "min_depth_m"),
        max_depth_m=_positive_finite(section.get("max_depth_m", 3.0), "max_depth_m"),
        confirmation_frames=confirmation_frames,
        confirmation_window=confirmation_window,
        confirmation_translation_m=_positive_finite(
            section.get("confirmation_translation_m", 0.03),
            "confirmation_translation_m",
        ),
        confirmation_rotation_deg=_positive_finite(
            section.get("confirmation_rotation_deg", 3.0),
            "confirmation_rotation_deg",
        ),
        measurement_translation_std_m=_positive_finite(
            section.get("measurement_translation_std_m", 0.01),
            "measurement_translation_std_m",
        ),
        measurement_rotation_std_deg=_positive_finite(
            section.get("measurement_rotation_std_deg", 1.0),
            "measurement_rotation_std_deg",
        ),
        targets=targets,
    )


def grayscale_marker_image(message) -> np.ndarray:
    """Decode a ROS Image or CompressedImage payload to owned grayscale."""

    if hasattr(message, "format") and not hasattr(message, "height"):
        raw = np.frombuffer(message.data, dtype=np.uint8)
        if raw.size == 0:
            raise ValueError("empty compressed marker image")
        image = cv2.imdecode(raw, cv2.IMREAD_GRAYSCALE)
        if image is None or image.size == 0:
            raise ValueError("invalid compressed marker image")
        return image

    height, width, step = int(message.height), int(message.width), int(message.step)
    if height <= 0 or width <= 0 or step <= 0:
        raise ValueError("invalid marker image dimensions")
    raw = np.frombuffer(message.data, dtype=np.uint8)
    encoding = str(message.encoding).lower()
    if encoding in {"mono8", "8uc1"}:
        if step < width or raw.size < height * step:
            raise ValueError("invalid mono8 marker image")
        return raw[: height * step].reshape(height, step)[:, :width].copy()
    channels = {"rgb8": 3, "bgr8": 3, "rgba8": 4, "bgra8": 4}.get(encoding)
    if channels is not None:
        if step < width * channels or raw.size < height * step:
            raise ValueError(f"invalid {encoding} marker image")
        image = raw[: height * step].reshape(height, step)[:, : width * channels]
        image = image.reshape(height, width, channels)
        conversion = {
            "rgb8": cv2.COLOR_RGB2GRAY,
            "bgr8": cv2.COLOR_BGR2GRAY,
            "rgba8": cv2.COLOR_RGBA2GRAY,
            "bgra8": cv2.COLOR_BGRA2GRAY,
        }[encoding]
        return cv2.cvtColor(image, conversion)
    if encoding == "nv12":
        total_rows, remainder = divmod(raw.size, step)
        if step < width or remainder or total_rows <= 0 or (total_rows * 2) % 3:
            raise ValueError("invalid NV12 marker image")
        luma_height = total_rows * 2 // 3
        if luma_height < height:
            raise ValueError("truncated NV12 marker image")
        return raw[: height * step].reshape(height, step)[:, :width].copy()
    raise ValueError(f"unsupported marker image encoding: {message.encoding}")


class MultiCubeMarkerEstimator:
    """Detect ArUco IDs once, then solve one rigid PnP per configured cube."""

    def __init__(self, config: CubeMarkerConfig) -> None:
        self.config = config
        dictionary_id = getattr(cv2.aruco, config.dictionary_name, None)
        if dictionary_id is None:
            raise ValueError(f"unknown ArUco dictionary: {config.dictionary_name}")
        dictionary = cv2.aruco.getPredefinedDictionary(dictionary_id)
        self._detector = cv2.aruco.ArucoDetector(
            dictionary, cv2.aruco.DetectorParameters()
        )

    def detect(
        self, image_gray: np.ndarray, camera_matrix: np.ndarray
    ) -> dict[str, CubePoseEstimate]:
        corners, ids, _ = self._detector.detectMarkers(image_gray)
        detections: dict[int, np.ndarray] = {}
        if ids is not None:
            for marker_corners, marker_id in zip(corners, ids.reshape(-1)):
                detections[int(marker_id)] = np.asarray(
                    marker_corners, dtype=np.float64
                ).reshape(4, 2)
        return self.estimate(detections, camera_matrix)

    def estimate(
        self,
        detections: Mapping[int, Sequence[Sequence[float]]],
        camera_matrix: np.ndarray,
    ) -> dict[str, CubePoseEstimate]:
        results: dict[str, CubePoseEstimate] = {}
        intrinsic = np.asarray(camera_matrix, dtype=np.float64).reshape(3, 3)
        if not np.all(np.isfinite(intrinsic)) or intrinsic[0, 0] <= 0.0:
            raise ValueError("invalid marker camera matrix")
        for camera, target in self.config.targets.items():
            visible = sorted(set(detections) & set(target.marker_corners_cube_m))
            if len(visible) < self.config.min_markers:
                continue
            object_points = np.concatenate(
                [target.marker_corners_cube_m[marker_id] for marker_id in visible]
            ).astype(np.float64)
            image_points = np.concatenate(
                [np.asarray(detections[marker_id], dtype=np.float64).reshape(4, 2) for marker_id in visible]
            )
            estimate = self._solve_target(
                camera, visible, object_points, image_points, intrinsic
            )
            if estimate is not None:
                results[camera] = estimate
        return results

    def _solve_target(
        self,
        camera: str,
        marker_ids: Sequence[int],
        object_points: np.ndarray,
        image_points: np.ndarray,
        camera_matrix: np.ndarray,
    ) -> Optional[CubePoseEstimate]:
        if len(marker_ids) == 1:
            ok, rvec, tvec = cv2.solvePnP(
                object_points,
                image_points,
                camera_matrix,
                None,
                flags=cv2.SOLVEPNP_IPPE,
            )
            inliers = np.arange(len(object_points), dtype=np.int64)
        else:
            ok, rvec, tvec, inlier_payload = cv2.solvePnPRansac(
                object_points,
                image_points,
                camera_matrix,
                None,
                flags=cv2.SOLVEPNP_EPNP,
                iterationsCount=200,
                reprojectionError=float(self.config.max_reprojection_error_px),
                confidence=0.999,
            )
            if not ok or inlier_payload is None:
                return None
            inliers = inlier_payload.reshape(-1)
        if not ok:
            return None
        inlier_ratio = len(inliers) / float(len(object_points))
        if inlier_ratio < self.config.min_inlier_ratio:
            return None
        rvec, tvec = cv2.solvePnPRefineLM(
            object_points[inliers],
            image_points[inliers],
            camera_matrix,
            None,
            rvec,
            tvec,
        )
        rotation, _ = cv2.Rodrigues(rvec)
        camera_points = object_points @ rotation.T + tvec.reshape(1, 3)
        depths = camera_points[:, 2]
        if (
            float(np.min(depths)) < self.config.min_depth_m
            or float(np.max(depths)) > self.config.max_depth_m
        ):
            return None
        projected, _ = cv2.projectPoints(
            object_points[inliers], rvec, tvec, camera_matrix, None
        )
        errors = np.linalg.norm(
            projected.reshape(-1, 2) - image_points[inliers], axis=1
        )
        median_error = float(np.median(errors))
        max_error = float(np.max(errors))
        if (
            median_error > self.config.max_reprojection_error_px
            or max_error > self.config.max_reprojection_error_px * 2.0
        ):
            return None
        rgb_from_cube = np.eye(4, dtype=np.float64)
        rgb_from_cube[:3, :3] = rotation
        rgb_from_cube[:3, 3] = tvec.reshape(3)
        return CubePoseEstimate(
            camera=camera,
            rgb_from_cube=rgb_from_cube,
            marker_ids=tuple(int(value) for value in marker_ids),
            corners=int(len(object_points)),
            inliers=int(len(inliers)),
            inlier_ratio=float(inlier_ratio),
            median_reprojection_error_px=median_error,
            max_reprojection_error_px=max_error,
        )


def marker_map_to_odom(
    map_from_head_center: np.ndarray,
    head_center_from_rgb: np.ndarray,
    rgb_from_cube: np.ndarray,
    cube_from_camera_center: np.ndarray,
    odom_from_camera_center: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Return marker-derived ``T_map_camera`` and correction ``T_map_odom``."""

    map_from_camera = (
        np.asarray(map_from_head_center, dtype=np.float64).reshape(4, 4)
        @ np.asarray(head_center_from_rgb, dtype=np.float64).reshape(4, 4)
        @ np.asarray(rgb_from_cube, dtype=np.float64).reshape(4, 4)
        @ np.asarray(cube_from_camera_center, dtype=np.float64).reshape(4, 4)
    )
    map_from_odom = map_from_camera @ np.linalg.inv(
        np.asarray(odom_from_camera_center, dtype=np.float64).reshape(4, 4)
    )
    return map_from_camera, map_from_odom
