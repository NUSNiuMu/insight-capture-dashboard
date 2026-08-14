"""Bridge sparse mapping status into compact web snapshots."""

from __future__ import annotations

import json
import threading
import time
from typing import Dict

try:
    from std_msgs.msg import String
    from std_srvs.srv import Empty
except ImportError:  # pragma: no cover - only fake/non-ROS imports use this path
    String = None
    Empty = None


class MappingStream:
    """Hold lightweight map counters and status without forwarding geometry."""

    def __init__(self, owner) -> None:
        self.owner = owner
        self._lock = threading.Lock()
        self._map_point_count = 0
        self._camera_roles: Dict[str, str] = {}
        self._camera_labels: Dict[str, str] = {}
        self._status_topics: Dict[str, str] = {}
        self._statuses: Dict[str, Dict[str, object]] = {}
        self._last_received: Dict[str, float] = {}
        self._subscriptions = []
        self._mapper_reset_client = None
        self._localizer_reset_client = None

    def start(self) -> None:
        if String is None or Empty is None:
            return
        self._camera_roles = {
            pose.name: pose.teleop_role
            for pose in self.owner.poses
            if pose.teleop_role in {"head", "left_hand", "right_hand"}
        }
        self._camera_labels = {
            camera.name: camera.label for camera in self.owner.cameras
        }
        self._status_topics = {
            name: (
                "/insight9_sparse_map/status"
                if role == "head"
                else f"/insight_global/{name}/status"
            )
            for name, role in self._camera_roles.items()
        }
        self._statuses = {
            name: {"state": "unavailable"} for name in self._status_topics
        }
        self._last_received = {name: 0.0 for name in self._status_topics}
        kwargs = {
            "callback_group": self.owner.ros_callback_group,
            "event_callbacks": self.owner.subscription_event_callbacks,
        }
        for name, topic in self._status_topics.items():
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
            "Mapping status bridge subscribed without receiving sparse cloud data"
        )

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
                if self._camera_roles.get(name) == "head":
                    try:
                        self._map_point_count = max(
                            0, int(status.get("map_point_count", self._map_point_count))
                        )
                    except (TypeError, ValueError):
                        pass
                self._last_received[name] = time.monotonic()

        return callback

    def snapshot(self) -> Dict[str, object]:
        now = time.monotonic()
        with self._lock:
            statuses = {}
            for name, value in self._statuses.items():
                status = dict(value)
                age = now - self._last_received[name]
                status["online"] = self._last_received[name] > 0.0 and age <= 2.0
                statuses[name] = status
            payload: Dict[str, object] = {
                "type": "mapping_update",
                "map_point_count": self._map_point_count,
                "statuses": statuses,
                "camera_roles": dict(self._camera_roles),
                "camera_labels": dict(self._camera_labels),
            }
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
            self._map_point_count = 0
            for name in self._statuses:
                self._statuses[name] = {"state": "resetting"}
        return {
            "ok": not unavailable,
            "requested": requested,
            "unavailable": unavailable,
            "mapping": self.snapshot(),
        }
