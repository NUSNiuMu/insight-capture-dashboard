"""把独立 ROS 建图进程的状态桥接为 Dashboard 使用的轻量快照。

Dashboard 不订阅稀疏点云和完整 Path，只接收三个 JSON 状态话题并调用统一 reset
服务。这样网页状态查询不会复制高维描述子点云，也不会给录制主进程增加几何负载。
"""

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


STATUS_TOPICS = {
    "insight9": "/insight9_sparse_map/status",
    "insight3_a": "/insight_global/insight3_a/status",
    "insight3_b": "/insight_global/insight3_b/status",
}
class MappingStream:
    """保存地图计数与在线状态，不转发三维几何数据。"""

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
        self._reset_client = None

    def start(self) -> None:
        if String is None or Empty is None:
            return
        # 与 Dashboard 其余 ROS 实体共用 callback group 和订阅事件诊断。
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
        self._reset_client = self.owner.create_client(
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

    def request_reset(self, timeout_sec: float = 5.0) -> Dict[str, object]:
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
        if not finished.wait(timeout_sec):
            return {"ok": False, "error": "mapping_reset_timeout"}
        try:
            future.result()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "mapping": self.snapshot()}
