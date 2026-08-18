"""Sustained live-QC monitoring and voice alert delivery."""

from __future__ import annotations

import shutil
import threading
import time
from collections import deque
from typing import Dict, Optional


class VoiceAlertQueue:
    def __init__(self, maxlen: int = 100) -> None:
        self._items = deque(maxlen=maxlen)
        self._next_id = 1
        self._lock = threading.Lock()

    def publish(self, text: str, *, code: str, severity: str = "critical") -> Dict:
        with self._lock:
            item = {
                "id": self._next_id,
                "epoch_s": time.time(),
                "code": code,
                "severity": severity,
                "text": text,
            }
            self._next_id += 1
            self._items.append(item)
            return dict(item)

    def since(self, cursor: int) -> list[Dict]:
        with self._lock:
            return [dict(item) for item in self._items if int(item["id"]) > int(cursor)]


class ActiveQcMonitor:
    """Record only sustained faults; raw capture continues for forensics."""

    def __init__(self, node, recording_manager, take_store, alerts: VoiceAlertQueue, config: Optional[Dict] = None) -> None:
        settings = dict(config or {})
        self.node = node
        self.recording_manager = recording_manager
        self.take_store = take_store
        self.alerts = alerts
        self.sustain_sec = max(0.5, float(settings.get("sustain_sec", 3.0)))
        self.camera_stale_sec = max(0.5, float(settings.get("camera_stale_sec", 2.0)))
        self.minimum_free_gb = max(0.0, float(settings.get("minimum_free_gb", 2.0)))
        self._pending_since: Dict[str, float] = {}
        self._active: set[str] = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="active_capture_qc")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self.poll()
            except Exception as exc:  # noqa: BLE001 - QC must not affect recording
                logger = getattr(self.node, "get_logger", lambda: None)()
                if logger is not None:
                    logger.warning(f"active QC poll failed: {exc}")

    def _observe(self, code: str, present: bool, text: str, **details: object) -> None:
        now = time.monotonic()
        if not present:
            self._pending_since.pop(code, None)
            if code in self._active:
                self._active.remove(code)
                self.take_store.add_anomaly({
                    "code": code,
                    "severity": "info",
                    "event": "resolved",
                    "details": details,
                })
            return
        started = self._pending_since.setdefault(code, now)
        if code in self._active or now - started < self.sustain_sec:
            return
        self._active.add(code)
        anomaly = {
            "code": code,
            "severity": "critical",
            "event": "started",
            "message": text,
            "details": details,
        }
        self.take_store.add_anomaly(anomaly)
        self.alerts.publish(text, code=code)

    def poll(self) -> None:
        if not self.recording_manager.is_recording():
            self._pending_since.clear()
            self._active.clear()
            return
        now = time.monotonic()
        for camera in self.node.cameras:
            with self.node.camera_input_lock:
                samples = list(self.node.camera_input_times.get(camera.name, []))
            age = None if not samples else now - samples[-1]
            self._observe(
                f"camera_stale:{camera.name}",
                age is None or age > self.camera_stale_sec,
                f"{camera.label}图像中断，本条已记录异常。",
                camera=camera.name,
                age_sec=age,
            )

        mapping = self.node.build_mapping_payload()
        statuses = mapping.get("statuses") if isinstance(mapping, dict) else {}
        statuses = statuses if isinstance(statuses, dict) else {}
        for name in ("insight9", "insight3_a", "insight3_b"):
            status = statuses.get(name) if isinstance(statuses.get(name), dict) else {}
            broken = not status.get("online") or status.get("state") == "error"
            if name.startswith("insight3"):
                broken = broken or not status.get("localized")
            self._observe(
                f"localization:{name}", broken,
                f"{name}定位异常，本条已标记复检。", service=name
            )

        bridge = getattr(self.node, "_recording_bridge", None)
        audit = bridge.snapshot_image_header_audit() if bridge is not None else {}
        for topic, stat in (audit.get("topics") or {}).items():
            missing = int(stat.get("missing", 0))
            self._observe(
                f"frame_loss:{topic}", missing > 0,
                "检测到持续丢帧，本条已标记复检。", topic=topic, missing=missing
            )

        status = self.recording_manager.status()
        storage = status.get("storage") or {}
        self._observe(
            "storage_fallback", bool(storage.get("using_fallback")),
            "录制存储已切换到备用盘，本条已记录异常。",
            reason=storage.get("fallback_reason"),
        )
        try:
            free = shutil.disk_usage(self.recording_manager.rosbag_root).free
        except OSError:
            free = 0
        self._observe(
            "storage_low", free < self.minimum_free_gb * 1024**3,
            "录制存储空间不足，请尽快停止采集。", free_bytes=free
        )
        output = "\n".join(status.get("recent_output") or []).lower()
        self._observe(
            "recorder_io", any(token in output for token in ("input/output error", "writer failure", "message loss")),
            "录制写入出现异常，本条已标记复检。",
        )
