"""Bridge sparse mapping status into compact web snapshots."""

from __future__ import annotations

import json
import threading
import time
from typing import Dict

try:
    from std_msgs.msg import String
    from std_srvs.srv import Empty, Trigger
except ImportError:  # pragma: no cover - only fake/non-ROS imports use this path
    String = None
    Empty = None
    Trigger = None


STATUS_TOPICS = {
    "insight9": "/insight9_sparse_map/status",
    "insight3_a": "/insight_global/insight3_a/status",
    "insight3_b": "/insight_global/insight3_b/status",
}
class MappingStream:
    """Hold lightweight map counters and status without forwarding geometry."""

    def __init__(self, owner) -> None:
        self.owner = owner
        self._lock = threading.Lock()
        self._map_point_count = 0
        self._statuses: Dict[str, Dict[str, object]] = {
            name: {"state": "unavailable"} for name in STATUS_TOPICS
        }
        self._last_received: Dict[str, float] = {
            name: 0.0 for name in STATUS_TOPICS
        }
        self._subscriptions = []
        self._mapper_reset_client = None
        self._localizer_reset_client = None
        self._capture_reference_client = None

    def start(self) -> None:
        if String is None or Empty is None or Trigger is None:
            return
        kwargs = {
            "callback_group": self.owner.ros_callback_group,
            "event_callbacks": self.owner.subscription_event_callbacks,
        }
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
        self._capture_reference_client = self.owner.create_client(
            Trigger,
            "/insight9_sparse_map/freeze_capture_reference",
            callback_group=self.owner.ros_callback_group,
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
                if name == "insight9":
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

    def freeze_capture_reference(self, timeout_sec: float = 3.0) -> Dict[str, object]:
        """Ask the mapper to pin a natural-feature map for batch validation."""

        client = self._capture_reference_client
        if client is None or not client.service_is_ready():
            return {
                "ok": False,
                "reason": "Insight9 capture-reference service is unavailable",
            }
        future = client.call_async(Trigger.Request())
        deadline = time.monotonic() + max(0.1, float(timeout_sec))
        while not future.done() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not future.done():
            return {"ok": False, "reason": "Insight9 capture-reference request timed out"}
        try:
            response = future.result()
        except Exception as exc:  # pragma: no cover - depends on rclpy transport
            return {"ok": False, "reason": f"Insight9 capture-reference failed: {exc}"}
        if response is None:
            return {"ok": False, "reason": "Insight9 capture-reference returned no response"}
        try:
            payload = json.loads(response.message or "{}")
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if not response.success:
            reason = str(payload.get("reason") or response.message or "reference rejected")
            return {"ok": False, "reason": reason, "details": payload}
        return {"ok": True, "reference": payload}
