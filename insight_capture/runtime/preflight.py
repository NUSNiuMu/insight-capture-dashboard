"""Recording preflight checks for the headless field runtime."""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Dict, Optional, Sequence

from insight_capture.runtime.recording.storage import probe_recording_root
from insight_capture.runtime.recording.recorder import VIO_CALIBRATION_CAMERA_NAMES
from insight_capture.runtime.camera_health import evaluate_cameras


class CapturePreflight:
    def __init__(self, node, recording_manager, config: Optional[Dict] = None) -> None:
        settings = dict(config or {})
        self.node = node
        self.recording_manager = recording_manager
        self.camera_stale_sec = max(0.2, float(settings.get("camera_stale_sec", 2.0)))
        self.pose_stale_sec = max(0.2, float(settings.get("pose_stale_sec", 2.0)))
        self.minimum_free_gb = max(0.0, float(settings.get("minimum_free_gb", 5.0)))
        self.require_primary_storage = bool(settings.get("require_primary_storage", False))
        self.require_mapping = bool(settings.get("require_mapping", True))
        self.require_localization = bool(settings.get("require_localization", True))

    @staticmethod
    def _failure(code: str, message: str, **details: object) -> Dict:
        return {"code": code, "message": message, "details": details}

    def evaluate(
        self,
        topics: Optional[Sequence[str]] = None,
        *,
        refresh_topics: bool = True,
        mode: str = "capture",
    ) -> Dict:
        now = time.monotonic()
        calibration_mode = mode == "vio_calibration"
        failures = []
        warnings = []
        camera_health = {}
        if getattr(self.node, "fake_pose", False):
            return {
                "type": "capture_preflight",
                "ok": True,
                "checked_at_epoch_s": time.time(),
                "failures": [],
                "warnings": [],
                "camera_health": {},
                "mapping": {},
                "storage": {},
                "topics": {},
                "fake": True,
                "mode": mode,
            }

        configured_camera_names = {camera.name for camera in self.node.cameras}
        if calibration_mode:
            missing_cameras = sorted(
                set(VIO_CALIBRATION_CAMERA_NAMES) - configured_camera_names
            )
            if missing_cameras:
                failures.append(
                    self._failure(
                        "camera_count",
                        "Insight3校准相机配置不完整",
                        cameras=missing_cameras,
                    )
                )
        elif len(self.node.cameras) != 3:
            failures.append(self._failure(
                "camera_count", f"应有三台相机，当前配置{len(self.node.cameras)}台"
            ))
        if not calibration_mode:
            camera_health, camera_failures = evaluate_cameras(
                self.node,
                self.camera_stale_sec,
                now=now,
            )
            failures.extend(camera_failures)
        # Raw-only calibration intentionally stops the rectified Dashboard
        # streams. Its route verifies all raw topics and consumes one payload
        # from each stereo image topic before starting the recorder.

        mapping = {} if calibration_mode else self.node.build_mapping_payload()
        statuses = mapping.get("statuses") if isinstance(mapping, dict) else {}
        statuses = statuses if isinstance(statuses, dict) else {}
        insight9 = statuses.get("insight9") if isinstance(statuses.get("insight9"), dict) else {}
        if (
            not calibration_mode
            and self.require_mapping
            and (not insight9.get("online") or insight9.get("state") == "error")
        ):
            failures.append(self._failure("mapping_not_ready", "Insight9建图状态未就绪"))
        if not calibration_mode and self.require_localization:
            for name in ("insight3_a", "insight3_b"):
                status = statuses.get(name) if isinstance(statuses.get(name), dict) else {}
                if not status.get("online") or not status.get("localized"):
                    failures.append(self._failure(
                        "localization_not_ready", f"{name}全局定位未就绪", camera=name
                    ))

        for pose in (() if calibration_mode else self.node.poses):
            received = float(self.node.last_pose_received_time.get(pose.name, 0.0))
            age = None if received <= 0 else max(0.0, now - received)
            if age is None or age > self.pose_stale_sec:
                failures.append(self._failure(
                    "pose_stale", f"{pose.name}位姿数据不新鲜", camera=pose.name, age_sec=age
                ))

        root = Path(self.recording_manager.rosbag_root)
        storage_error = probe_recording_root(root)
        try:
            free_bytes = shutil.disk_usage(root).free
        except OSError as exc:
            free_bytes = 0
            storage_error = storage_error or str(exc)
        storage = {
            "path": str(root),
            "writable": storage_error is None,
            "error": storage_error,
            "free_bytes": free_bytes,
            "minimum_free_bytes": int(self.minimum_free_gb * 1024**3),
            **dict(getattr(self.recording_manager, "storage_status", {}) or {}),
        }
        if storage_error:
            failures.append(self._failure("storage_unwritable", f"录制存储不可写：{storage_error}"))
        if storage.get("using_fallback"):
            fallback_issue = self._failure(
                "storage_fallback",
                "录制盘未正确挂载，当前正在使用备用存储",
                reason=storage.get("fallback_reason"),
            )
            if self.require_primary_storage:
                failures.append(fallback_issue)
            else:
                warnings.append(fallback_issue)
        if free_bytes < self.minimum_free_gb * 1024**3:
            failures.append(self._failure(
                "storage_low", f"录制存储剩余空间不足{self.minimum_free_gb:g}GB", free_bytes=free_bytes
            ))

        selected = list(topics or self.recording_manager.default_topics)
        try:
            catalog = self.recording_manager.current_topic_catalog(refresh=refresh_topics)
        except Exception as exc:  # noqa: BLE001 - preflight reports a stable failure
            catalog = {"topics": []}
            failures.append(self._failure("topic_discovery_failed", f"录制话题检查失败：{exc}"))
        available = set(catalog.get("topics") or [])
        required_topics = (
            {topic for topic in selected if topic != "/tf_static"}
            if calibration_mode
            else {
                *(camera.topic for camera in self.node.cameras),
                *(pose.topic for pose in self.node.poses),
                *(topic for topic in selected if topic.endswith(("/imu", "/vio_100hz"))),
            }
        )
        missing = sorted(
            topic for topic in required_topics
            if topic != "/tf_static" and topic not in available
        )
        optional_missing = sorted(
            topic for topic in selected
            if topic != "/tf_static" and topic not in available and topic not in required_topics
        )
        if missing:
            failures.append(self._failure(
                "recorder_topics_missing", f"{len(missing)}个必要录制话题无发布者", topics=missing
            ))

        return {
            "type": "capture_preflight",
            "mode": mode,
            "ok": not failures,
            "checked_at_epoch_s": time.time(),
            "failures": failures,
            "warnings": warnings,
            "camera_health": camera_health,
            "mapping": mapping,
            "storage": storage,
            "topics": {
                "selected": selected,
                "required": sorted(required_topics),
                "missing": missing,
                "optional_missing": optional_missing,
            },
        }

    _CAMERA_SPEECH_NAMES = {
        "insight3_a": "右手相机",
        "insight3_b": "左手相机",
        "insight9_a": "头部相机",
    }

    @classmethod
    def speech(cls, report: Dict) -> str:
        failures = report.get("failures") or []
        if not failures:
            storage = report.get("storage") or {}
            free_gb = float(storage.get("free_bytes", 0)) / 1024**3
            if report.get("mode") == "vio_calibration":
                return f"两台Insight3未校正图像和录制数据流正常，剩余空间{free_gb:.0f}GB，可以开始校准录制。"
            if storage.get("using_fallback"):
                return f"当前使用备用存储，剩余空间{free_gb:.0f}GB，可以开始采集。"
            return f"三台相机、定位和录制数据流正常，剩余空间{free_gb:.0f}GB，可以开始采集。"
        stale_cameras = [
            str(item.get("details", {}).get("camera"))
            for item in failures
            if item.get("code") == "camera_stale"
        ]
        if stale_cameras:
            names = "、".join(cls._CAMERA_SPEECH_NAMES.get(name, name) for name in stale_cameras)
            return f"{names}未连接，请检查相机。"
        codes = {str(item.get("code")) for item in failures}
        if codes & {"mapping_not_ready", "localization_not_ready"}:
            return "目前没有校准，请说开始校准。"
        messages = [str(item.get("message") or "未知异常") for item in failures[:3]]
        return "无法开始录制，" + "；".join(messages) + "。"
