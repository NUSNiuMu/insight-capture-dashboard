"""Bridge sparse mapping ROS topics into compact web visualization snapshots."""

from __future__ import annotations

import json
import threading
import time
from typing import Dict, Optional

import numpy as np

try:
    from nav_msgs.msg import Path as PathMsg
    from sensor_msgs.msg import PointCloud2
    from std_msgs.msg import String
    from std_srvs.srv import Empty
except ImportError:  # pragma: no cover - only fake/non-ROS imports use this path
    PathMsg = None
    PointCloud2 = None
    String = None
    Empty = None


PATH_TOPICS = {
    "insight9": "/insight9_sparse_map/path",
    "insight3_a": "/insight_global/insight3_a/path",
    "insight3_b": "/insight_global/insight3_b/path",
}
STATUS_TOPICS = {
    "insight9": "/insight9_sparse_map/status",
    "insight3_a": "/insight_global/insight3_a/status",
    "insight3_b": "/insight_global/insight3_b/status",
}


class MappingStream:
    """Hold bounded map/path snapshots without adding work to image callbacks."""

    def __init__(self, owner, *, max_map_points: int = 12_000) -> None:
        self.owner = owner
        self.max_map_points = max(100, int(max_map_points))
        self._lock = threading.Lock()
        self._map_points: list[list[float]] = []
        self._map_version = 0
        self._paths: Dict[str, list[list[float]]] = {
            name: [] for name in PATH_TOPICS
        }
        self._path_versions: Dict[str, int] = {
            name: 0 for name in PATH_TOPICS
        }
        self._statuses: Dict[str, Dict[str, object]] = {
            name: {"state": "unavailable"} for name in STATUS_TOPICS
        }
        self._last_received: Dict[str, float] = {
            name: 0.0 for name in STATUS_TOPICS
        }
        self._subscriptions = []
        self._mapper_reset_client = None
        self._localizer_reset_client = None

    def start(self) -> None:
        if PointCloud2 is None or PathMsg is None or String is None or Empty is None:
            return
        kwargs = {
            "callback_group": self.owner.ros_callback_group,
            "event_callbacks": self.owner.subscription_event_callbacks,
        }
        self._subscriptions.append(
            self.owner.create_subscription(
                PointCloud2,
                "/insight9_sparse_map/points",
                self._on_map,
                1,
                **kwargs,
            )
        )
        for name, topic in PATH_TOPICS.items():
            self._subscriptions.append(
                self.owner.create_subscription(
                    PathMsg, topic, self._path_callback(name), 1, **kwargs
                )
            )
        for name, topic in STATUS_TOPICS.items():
            self._subscriptions.append(
                self.owner.create_subscription(
                    String, topic, self._status_callback(name), 1, **kwargs
                )
            )
        self._mapper_reset_client = self.owner.create_client(
            Empty, "/insight9_sparse_map/reset", callback_group=self.owner.ros_callback_group
        )
        self._localizer_reset_client = self.owner.create_client(
            Empty, "/insight_global/reset", callback_group=self.owner.ros_callback_group
        )
        self.owner.dashboard_subscriptions.extend(self._subscriptions)
        self.owner.get_logger().info(
            "Mapping web stream subscribed to sparse map and three global paths"
        )

    @staticmethod
    def _decode_xyz(message: PointCloud2) -> np.ndarray:
        fields = {field.name: field for field in message.fields}
        if any(name not in fields for name in ("x", "y", "z")):
            raise ValueError("point cloud has no XYZ fields")
        count = int(message.width) * int(message.height)
        point_step = int(message.point_step)
        if count <= 0:
            return np.empty((0, 3), dtype=np.float32)
        if point_step <= 0 or len(message.data) < count * point_step:
            raise ValueError("point cloud payload is truncated")
        endian = ">" if message.is_bigendian else "<"
        dtype = np.dtype(
            {
                "names": ["x", "y", "z"],
                "formats": [f"{endian}f4", f"{endian}f4", f"{endian}f4"],
                "offsets": [
                    int(fields["x"].offset),
                    int(fields["y"].offset),
                    int(fields["z"].offset),
                ],
                "itemsize": point_step,
            }
        )
        packed = np.frombuffer(message.data, dtype=dtype, count=count)
        points = np.column_stack((packed["x"], packed["y"], packed["z"])).astype(
            np.float32, copy=False
        )
        return points[np.isfinite(points).all(axis=1)]

    def _on_map(self, message: PointCloud2) -> None:
        try:
            points = self._decode_xyz(message)
        except ValueError as exc:
            self.owner.get_logger().warning(f"Mapping web cloud rejected: {exc}")
            return
        if len(points) > self.max_map_points:
            indices = np.linspace(
                0, len(points) - 1, self.max_map_points, dtype=np.int64
            )
            points = points[indices]
        rounded = np.round(points, 4).tolist()
        with self._lock:
            self._map_points = rounded
            self._map_version += 1

    def _path_callback(self, name: str):
        def callback(message: PathMsg) -> None:
            points = [
                [
                    round(float(pose.pose.position.x), 4),
                    round(float(pose.pose.position.y), 4),
                    round(float(pose.pose.position.z), 4),
                ]
                for pose in message.poses
            ]
            with self._lock:
                self._paths[name] = points
                self._path_versions[name] += 1

        return callback

    def _status_callback(self, name: str):
        def callback(message: String) -> None:
            try:
                status = json.loads(message.data)
                if not isinstance(status, dict):
                    raise ValueError("status is not an object")
            except (json.JSONDecodeError, ValueError):
                status = {"state": "error", "error": "invalid status payload"}
            with self._lock:
                self._statuses[name] = status
                self._last_received[name] = time.monotonic()

        return callback

    def snapshot(
        self, *, known_map_version: Optional[int] = None
    ) -> Dict[str, object]:
        now = time.monotonic()
        with self._lock:
            statuses = {}
            for name, value in self._statuses.items():
                status = dict(value)
                age = now - self._last_received[name]
                status["online"] = self._last_received[name] > 0.0 and age <= 2.0
                status["age_ms"] = (
                    None
                    if self._last_received[name] <= 0.0
                    else round(age * 1000.0)
                )
                statuses[name] = status
            payload: Dict[str, object] = {
                "type": "mapping_update",
                "map_version": self._map_version,
                "map_point_count": len(self._map_points),
                "paths": {name: list(points) for name, points in self._paths.items()},
                "path_versions": dict(self._path_versions),
                "statuses": statuses,
            }
            if known_map_version != self._map_version:
                payload["map_points"] = list(self._map_points)
            return payload

    def request_reset(self) -> Dict[str, object]:
        requested = []
        unavailable = []
        # Clear the localizer first so an empty/new mapper publication cannot race
        # with stale corrections left from the preceding session.
        for name, client in (
            ("localizer", self._localizer_reset_client),
            ("mapper", self._mapper_reset_client),
        ):
            if client is not None and client.service_is_ready():
                client.call_async(Empty.Request())
                requested.append(name)
            else:
                unavailable.append(name)
        with self._lock:
            self._map_points = []
            self._map_version += 1
            for name in self._paths:
                self._paths[name] = []
                self._path_versions[name] += 1
            for name in self._statuses:
                self._statuses[name] = {"state": "resetting"}
        return {
            "ok": not unavailable,
            "requested": requested,
            "unavailable": unavailable,
            "mapping": self.snapshot(),
        }
