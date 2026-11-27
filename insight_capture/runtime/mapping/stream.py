"""把独立 ROS 建图进程的状态桥接为 Dashboard 使用的轻量快照。

Dashboard 接收头部与腕部的 JSON 状态话题，并调用统一 reset 服务。
网页状态查询无需复制稀疏点云或完整 Path。
"""

from __future__ import annotations

from collections import deque
import json
import threading
import time
from typing import Callable, Dict, Optional

try:
    from std_msgs.msg import String
    from std_srvs.srv import Empty
except ImportError:  # pragma: no cover - only fake/non-ROS imports use this path
    String = None
    Empty = None


STATUS_TOPICS = {
    "insight9": "/insight9_sparse_map/status",
    "insight3_a": "/insight_global/insight3_a/status",
    "insight3_b": "/insight_global/insight3_b/status",
}


class MappingStream:
    """保存地图计数与在线状态，不转发三维几何数据。"""

    def __init__(self, owner) -> None:
        self.owner = owner
        self.status_topics = dict(STATUS_TOPICS)
        self._lock = threading.Lock()
        self._map_point_count = 0
        self._statuses: Dict[str, Dict[str, object]] = {
            name: {"state": "unavailable"} for name in STATUS_TOPICS
        }
        self._last_received: Dict[str, float] = {
            name: 0.0 for name in STATUS_TOPICS
        }
        self._subscriptions = []
        self._reset_client = None
        self._world_epoch = None
        self._camera_epochs = {}
        self._retired_sessions = deque(maxlen=16)

    def start(self) -> None:
        if String is None or Empty is None:
            return
        # 与 Dashboard 其余 ROS 实体共用 callback group 和订阅事件诊断。
        kwargs = {
            "callback_group": self.owner.ros_callback_group,
            "event_callbacks": self.owner.subscription_event_callbacks,
        }
        for pose in self.owner.poses:
            if getattr(pose, "visual_only", False) and pose.topic:
                self.status_topics[pose.name] = pose.topic.removesuffix("/pose") + "/status"
                self._statuses[pose.name] = {"state": "unavailable"}
                self._last_received[pose.name] = 0.
        for name, topic in self.status_topics.items():
            self._subscriptions.append(
                self.owner.create_subscription(
                    String, topic, self._status_callback(name), 1, **kwargs
                )
            )
        self._reset_client = self.owner.create_client(
            Empty, "/insight_global/reset", callback_group=self.owner.ros_callback_group
        )
        self.owner.dashboard_subscriptions.extend(self._subscriptions)
        self.owner.get_logger().info(
            "Mapping status bridge subscribed without receiving sparse cloud data"
        )

    def _sync_pose_epoch(self, name, status):
        # Keep status epochs ordered. The owner checks the timestamp boundary
        # under the same pose lock used for trajectory writes.
        session, generation = status.get("session_id"), status.get("generation")
        stamp = status.get("last_reset_wall_ns")
        if (not isinstance(session, str) or not session
                or type(generation) is not int or generation < 0
                or type(stamp) is not int or stamp < 0):
            return True
        if session in self._retired_sessions:
            return False
        world = (session, generation)
        world_changed = False
        if name == "insight9":
            previous_world = self._world_epoch
            if previous_world and session == previous_world[0] and generation < previous_world[1]:
                return False
            world_changed = previous_world != world
            if world_changed:
                if previous_world and session != previous_world[0]:
                    self._retired_sessions.append(previous_world[0])
                    self._camera_epochs.clear()
                self._world_epoch = world
                topics = {topic.removesuffix("/status") + "/pose" for topic in self.status_topics.values()}
                names = [p.name for p in self.owner.poses if p.topic in topics]
                self.owner.set_common_pose_epoch(names, stamp, reset=previous_world is not None)
        elif self._world_epoch is not None and world != self._world_epoch:
            # A new wrist status can precede the authoritative head status.
            return not (session == self._world_epoch[0] and generation < self._world_epoch[1])
        epoch, reset_stamp = status.get("tracking_epoch"), status.get("last_pose_reset_wall_ns")
        if type(epoch) is not int or epoch < 0 or type(reset_stamp) is not int or reset_stamp < 0:
            return True
        previous_camera = self._camera_epochs.get(name)
        if previous_camera and previous_camera[0] == session and epoch < previous_camera[1]:
            return False
        current = (session, epoch)
        if previous_camera != current:
            self._camera_epochs[name] = current
            topic = self.status_topics[name].removesuffix("/status") + "/pose"
            names = [p.name for p in self.owner.poses if p.topic == topic]
            # A newer camera epoch is authoritative even when ROS time moved
            # backwards; a max() with the old world time would lock out recovery.
            floor = reset_stamp if epoch > 0 or reset_stamp > 0 else stamp
            self.owner.set_common_pose_epoch(names, floor,
                reset=previous_camera is not None and not world_changed)
        return True

    def _status_callback(self, name: str):
        def callback(message: String) -> None:
            try:
                status = json.loads(message.data)
                if not isinstance(status, dict):
                    raise ValueError("status is not an object")
            except (json.JSONDecodeError, ValueError):
                status = {"state": "error", "error": "invalid status payload"}
            with self._lock:
                if not self._sync_pose_epoch(name, status):
                    return
                self._statuses[name] = status
                if name == "insight9":
                    try:
                        self._map_point_count = max(
                            0, int(status.get("map_point_count", self._map_point_count))
                        )
                    except (TypeError, ValueError):
                        pass
                self._last_received[name] = time.monotonic()
            events = getattr(self.owner, "events", None)
            if events is not None:
                state = str(status.get("state", "unavailable"))
                detail = str(status.get("error") or status.get("reason")
                             or "; ".join(status.get("missing_inputs", [])))
                if state == "tracking" and detail == "map_revision":
                    detail = ""
                events.changed("localization", name, state, detail,
                    level="error" if state == "error" else "info",
                    localized=status.get("localized", False),
                    missing_inputs=status.get("missing_inputs", []))

        return callback

    def snapshot(self) -> Dict[str, object]:
        now = time.monotonic()
        with self._lock:
            statuses = {}
            for name, value in self._statuses.items():
                status = dict(value)
                # “进程曾发过状态”不等于当前在线；超过两秒即标记离线。
                age = now - self._last_received[name]
                status["online"] = self._last_received[name] > 0.0 and age <= 2.0
                statuses[name] = status
            payload: Dict[str, object] = {
                "type": "mapping_update",
                "map_point_count": self._map_point_count,
                "statuses": statuses,
            }
            return payload

    def request_reset(
        self, timeout_sec: float = 5.0, *,
        on_dispatched: Optional[Callable[[], None]] = None,
    ) -> Dict[str, object]:
        try:
            client = self._reset_client
            if client is None or not client.service_is_ready():
                return {"ok": False, "error": "mapping_service_unavailable"}
            with self._lock:
                self._map_point_count = 0
                for name in self._statuses:
                    self._statuses[name] = {"state": "resetting"}
            future = client.call_async(Empty.Request())
            finished = threading.Event()
            future.add_done_callback(lambda _: finished.set())
        finally:
            # Only RPC admission shares the capture transition, not its response wait.
            if on_dispatched is not None:
                on_dispatched()
        if not finished.wait(timeout_sec):
            return {"ok": False, "error": "mapping_reset_timeout"}
        try:
            future.result()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        self.owner.clear_traces()
        return {"ok": True, "mapping": self.snapshot()}
