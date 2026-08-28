"""Durable Session/Take metadata for field capture."""

from __future__ import annotations

import json
import re
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Dict, Mapping, Optional

from .session import SessionMetadata
from .tasks import CaptureTask, CaptureTaskCatalog


def _slug(value: object, fallback: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z_-]+", "_", str(value or "").strip())
    return normalized.strip("_") or fallback


class SessionTakeStore:
    """Persist operator decisions and lightweight QC without touching raw bags."""

    def __init__(
        self,
        results_root: Path,
        config: Optional[Dict] = None,
        task_catalog: Optional[CaptureTaskCatalog] = None,
    ) -> None:
        self._lock = threading.RLock()
        self._current_take_id: Optional[int] = None
        self.results_root = Path(results_root).resolve()
        self.sessions_root = self.results_root / "sessions"
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        self._active_state_path = self.sessions_root / "active_session.json"
        self._task_catalog = task_catalog
        self._active = True
        self.task_id = ""
        self.task = "unspecified"
        self.task_instruction = ""
        self.task_speech_name = ""
        self.capture_profile = ""
        self.operator = "unknown"
        self.session_id = ""
        self.station_check_after_take = False
        self.root = self.sessions_root
        self.takes_root = self.sessions_root / "takes"
        if task_catalog is not None:
            self.operator = str((config or {}).get("operator") or "unknown")
            self._restore_task_session()
            return
        metadata = SessionMetadata.from_config(config, _slug)
        self._configure_legacy_session(metadata)
        self._write_session()

    def _configure_legacy_session(self, metadata: SessionMetadata) -> None:
        self.task = metadata.task
        self.task_id = _slug(metadata.task, "unspecified")
        self.operator = metadata.operator
        self.session_id = metadata.session_id
        self.station_check_after_take = metadata.station_check_after_take
        self.root = self.sessions_root / self.session_id
        self.takes_root = self.root / "takes"
        self.takes_root.mkdir(parents=True, exist_ok=True)

    def _configure_task_session(self, task: CaptureTask, session_id: str) -> None:
        self._active = True
        self.task_id = task.task_id
        self.task = task.name
        self.task_instruction = task.instruction
        self.task_speech_name = task.speech_name
        self.capture_profile = task.capture_profile
        self.session_id = session_id
        self.station_check_after_take = task.station_check_after_take
        self.root = self.sessions_root / session_id
        self.takes_root = self.root / "takes"
        self.takes_root.mkdir(parents=True, exist_ok=True)
        self._current_take_id = None

    @staticmethod
    def _task_set_id(task_id: str) -> str:
        return _slug(task_id, "task")

    def _migrate_active_task_set(self, previous_session_id: str, task_id: str) -> None:
        """Move the active dated metadata folder to the stable task-set folder once."""

        stable_id = self._task_set_id(task_id)
        if not previous_session_id or previous_session_id == stable_id:
            return
        previous_root = self.sessions_root / previous_session_id
        stable_root = self.sessions_root / stable_id
        if not previous_root.is_dir() or stable_root.exists():
            return
        previous_root.replace(stable_root)
        for path in [stable_root / "session.json", *(stable_root / "takes").glob("take_*.json")]:
            if not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            payload["session_id"] = stable_id
            self._write_json(path, payload)

    def _restore_task_session(self) -> None:
        state = None
        if self._active_state_path.is_file():
            try:
                state = json.loads(self._active_state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = None
        if isinstance(state, dict) and state.get("active"):
            task = self._task_catalog.get(str(state.get("task_id") or ""))
            previous_session_id = str(state.get("session_id") or "").strip()
            self._migrate_active_task_set(previous_session_id, task.task_id)
            self._configure_task_session(task, self._task_set_id(task.task_id))
            self._write_session()
            self._write_active_state()
            return
        if isinstance(state, dict):
            task_id = str(state.get("task_id") or "").strip()
            previous_session_id = str(state.get("session_id") or "").strip()
            if task_id:
                self._migrate_active_task_set(previous_session_id, task_id)
                self._configure_task_session(
                    self._task_catalog.get(task_id), self._task_set_id(task_id)
                )
            self._active = False
            self._write_active_state()
            return
        default_task = self._task_catalog.default()
        if default_task is not None:
            self.activate_task(default_task.task_id)
        else:
            self._active = False

    def _write_json(self, path: Path, payload: Dict) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(path)

    def _write_session(self, *, ended_at_epoch_s: Optional[float] = None) -> None:
        path = self.root / "session.json"
        created = time.time()
        if path.is_file():
            try:
                created = float(json.loads(path.read_text()).get("created_at_epoch_s", created))
            except (OSError, ValueError, TypeError):
                pass
        self._write_json(path, {
            "schema_version": 1,
            "session_id": self.session_id,
            "task": self.task,
            "task_id": self.task_id,
            "task_name": self.task,
            "task_instruction": self.task_instruction,
            "capture_profile": self.capture_profile,
            "operator": self.operator,
            "station_check_after_take": self.station_check_after_take,
            "state": "active" if self._active else "ended",
            "created_at_epoch_s": created,
            "updated_at_epoch_s": time.time(),
            "ended_at_epoch_s": ended_at_epoch_s,
        })

    def _write_active_state(self) -> None:
        self._write_json(
            self._active_state_path,
            {
                "schema_version": 1,
                "active": self._active,
                "task_id": self.task_id or None,
                "session_id": self.session_id or None,
                "updated_at_epoch_s": time.time(),
            },
        )

    def activate_task(self, task_id: str) -> Dict[str, object]:
        if self._task_catalog is None:
            raise RuntimeError("Capture task switching is not configured.")
        with self._lock:
            current = self.current()
            if current and current.get("state") in {"starting", "recording", "finalizing"}:
                raise RuntimeError("Cannot change task while a take is active.")
            task = self._task_catalog.get(task_id)
            if self._active and self.task_id == task.task_id:
                return self.task_status()
            self._configure_task_session(task, self._task_set_id(task.task_id))
            self._write_session()
            self._write_active_state()
            return self.task_status()

    def create_task(self, payload: Mapping[str, object]) -> Dict[str, object]:
        if self._task_catalog is None:
            raise RuntimeError("Capture task management is not configured.")
        with self._lock:
            task = self._task_catalog.create(payload)
            return task.as_dict()

    def update_task(
        self, task_id: str, payload: Mapping[str, object]
    ) -> Dict[str, object]:
        if self._task_catalog is None:
            raise RuntimeError("Capture task management is not configured.")
        with self._lock:
            current = self.current()
            if current and current.get("state") in {"starting", "recording", "finalizing"}:
                raise RuntimeError("Cannot edit a task while a take is active.")
            task = self._task_catalog.update(task_id, payload)
            if self.task_id == task.task_id:
                self.task = task.name
                self.task_instruction = task.instruction
                self.task_speech_name = task.speech_name
                self.capture_profile = task.capture_profile
                self.station_check_after_take = task.station_check_after_take
                self._write_session()
                self._write_active_state()
            return task.as_dict()

    def end_task(self) -> Dict[str, object]:
        with self._lock:
            if not self._active:
                return self.task_status()
            current = self.current()
            if current and current.get("state") in {"starting", "recording", "finalizing"}:
                raise RuntimeError("Cannot end task while a take is active.")
            ended_at = time.time()
            self._active = False
            self._write_session(ended_at_epoch_s=ended_at)
            self._write_active_state()
            return self.task_status()

    def list_tasks(self) -> list[Dict[str, object]]:
        if self._task_catalog is None:
            return []
        tasks = []
        for item in self._task_catalog.list():
            task_set_id = self._task_set_id(str(item.get("task_id") or ""))
            takes_root = self.sessions_root / task_set_id / "takes"
            takes = self._list_takes_from(takes_root)
            tasks.append({
                **item,
                "task_set_id": task_set_id,
                "recording_subdirectory": task_set_id,
                "active": self._active and self.task_id == item.get("task_id"),
                "stats": self._take_stats(takes, takes_root=takes_root),
            })
        return tasks

    def recording_subdirectory(self) -> Optional[str]:
        """Return the stable raw-data folder for the active task set."""

        with self._lock:
            return self._task_set_id(self.task_id) if self._active and self.task_id else None

    def _path(self, take_id: int) -> Path:
        return self.takes_root / f"take_{int(take_id):04d}.json"

    def _read(self, take_id: int) -> Dict:
        return json.loads(self._path(take_id).read_text(encoding="utf-8"))

    def _next_take_id(self) -> int:
        return self._next_take_id_from(self.takes_root)

    @staticmethod
    def _next_take_id_from(takes_root: Path) -> int:
        ids = []
        for path in takes_root.glob("take_*.json"):
            match = re.fullmatch(r"take_(\d+)\.json", path.name)
            if match:
                ids.append(int(match.group(1)))
        return max(ids, default=0) + 1

    def reserve_take(
        self, requested_bag_name: Optional[str] = None, *, trigger: str = "web"
    ) -> Dict:
        with self._lock:
            if not self._active:
                raise RuntimeError("No capture task is active.")
            if self._current_take_id is not None:
                current = self._read(self._current_take_id)
                if current.get("state") in {"starting", "recording", "finalizing"}:
                    raise RuntimeError("A take is already active.")
            take_id = self._next_take_id()
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            bag_name = _slug(
                requested_bag_name,
                f"{_slug(self.task_id, 'task')}_take_{take_id:04d}_{timestamp}",
            )
            payload = {
                "schema_version": 1,
                "session_id": self.session_id,
                "task": self.task,
                "task_id": self.task_id,
                "task_name": self.task,
                "task_instruction": self.task_instruction,
                "capture_profile": self.capture_profile,
                "take_id": take_id,
                "operator": self.operator,
                "trigger": str(trigger or "web"),
                "bag_name": bag_name,
                "bag_path": None,
                "state": "starting",
                "start_epoch_s": time.time(),
                "end_epoch_s": None,
                "quick_qc": {"state": "pending", "reasons": []},
                "station_check": {"required": self.station_check_after_take, "state": "pending"},
                "operator_valid": None,
                "operator_decision": "pending",
                "reject_reason": None,
                "anomaly_timeline": [],
            }
            self._write_json(self._path(take_id), payload)
            self._current_take_id = take_id
            return deepcopy(payload)

    def mark_recording(self, output_path: object) -> Dict:
        with self._lock:
            payload = self.current()
            if payload is None:
                raise RuntimeError("No take is reserved.")
            payload["state"] = "recording"
            payload["bag_path"] = str(output_path or "") or None
            self._write_json(self._path(payload["take_id"]), payload)
            return deepcopy(payload)

    def fail_start(self, reason: object) -> None:
        with self._lock:
            payload = self.current()
            if payload is None:
                return
            payload.update({
                "state": "start_failed",
                "end_epoch_s": time.time(),
                "quick_qc": {"state": "fail", "reasons": [str(reason)]},
            })
            self._write_json(self._path(payload["take_id"]), payload)

    def complete_current(self, recording_status: Dict) -> Optional[Dict]:
        with self._lock:
            payload = self.current()
            if payload is None:
                return None
            reasons = []
            if recording_status.get("merge_state") == "error":
                reasons.append(str(recording_status.get("merge_error") or "recording finalization failed"))
            network = recording_status.get("network_audit") or {}
            if isinstance(network, dict) and network.get("ok") is False:
                reasons.append("recording network audit failed")
            image_audit = recording_status.get("image_header_audit") or {}
            if (
                isinstance(image_audit, dict)
                and image_audit.get("recording_quality_authoritative") is True
                and image_audit.get("ok") is False
            ):
                reasons.append("image continuity audit failed")
            anomalies = payload.get("anomaly_timeline") or []
            quality_anomalies = [
                item for item in anomalies if item.get("affects_quality", True)
            ]
            if any(item.get("severity") == "critical" for item in quality_anomalies):
                reasons.append("critical live QC anomaly")
            qc_state = "fail" if recording_status.get("merge_state") == "error" else (
                "suspect" if reasons or quality_anomalies else "pass"
            )
            payload.update({
                "state": "complete" if qc_state != "fail" else "finalization_failed",
                "end_epoch_s": time.time(),
                "bag_path": str(recording_status.get("output_path") or payload.get("bag_path") or "") or None,
                "quick_qc": {
                    "state": qc_state,
                    "reasons": reasons,
                    "image_header_audit": image_audit or None,
                    "network_audit": network or None,
                },
            })
            self._write_json(self._path(payload["take_id"]), payload)
            return deepcopy(payload)

    def add_anomaly(self, anomaly: Dict) -> Optional[Dict]:
        with self._lock:
            payload = self.current()
            if payload is None or payload.get("state") not in {"recording", "finalizing"}:
                return None
            entry = dict(anomaly)
            entry.setdefault("epoch_s", time.time())
            payload.setdefault("anomaly_timeline", []).append(entry)
            self._write_json(self._path(payload["take_id"]), payload)
            return deepcopy(payload)

    def reject_current(self, reason: object = "operator_rejected") -> Dict:
        """Mark a take invalid; raw bag data is deliberately never deleted."""
        with self._lock:
            payload = self.current()
            if payload is None:
                raise RuntimeError("No take is available to reject.")
            payload.update({
                "operator_valid": False,
                "operator_decision": "rejected",
                "reject_reason": str(reason or "operator_rejected"),
                "operator_decided_at_epoch_s": time.time(),
            })
            self._write_json(self._path(payload["take_id"]), payload)
            return deepcopy(payload)

    def record_station_check(self, result: Dict) -> Optional[Dict]:
        with self._lock:
            payload = self.current()
            if payload is None:
                return None
            payload["station_check"] = {
                "required": self.station_check_after_take,
                "state": str(result.get("state") or "unknown"),
                "result": result,
                "checked_at_epoch_s": time.time(),
            }
            self._write_json(self._path(payload["take_id"]), payload)
            return deepcopy(payload)

    def current(self) -> Optional[Dict]:
        with self._lock:
            if not self._active:
                return None
            if self._current_take_id is None:
                paths = sorted(self.takes_root.glob("take_*.json"))
                if not paths:
                    return None
                try:
                    self._current_take_id = int(paths[-1].stem.split("_")[-1])
                except ValueError:
                    return None
            try:
                return deepcopy(self._read(self._current_take_id))
            except (OSError, ValueError):
                return None

    def list_takes(self) -> list[Dict]:
        return self._list_takes_from(self.takes_root)

    @staticmethod
    def _list_takes_from(takes_root: Path) -> list[Dict]:
        items = []
        for path in sorted(takes_root.glob("take_*.json"), reverse=True):
            try:
                items.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return items

    def snapshot(self) -> Dict:
        status = self.task_status()
        return {
            "session": {
                "session_id": self.session_id,
                "task": self.task,
                "task_id": self.task_id,
                "task_name": self.task,
                "task_instruction": self.task_instruction,
                "capture_profile": self.capture_profile,
                "operator": self.operator,
                "station_check_after_take": self.station_check_after_take,
                "recording_subdirectory": self.recording_subdirectory(),
                "active": self._active,
            },
            "current_take": self.current(),
            "takes": self.list_takes(),
            "stats": status.get("stats", {}),
        }

    def _take_stats(
        self, takes: list[Dict], *, takes_root: Optional[Path] = None
    ) -> Dict[str, int]:
        recorded = [take for take in takes if take.get("bag_path")]
        completed = [take for take in recorded if take.get("end_epoch_s")]
        rejected = [take for take in recorded if take.get("operator_valid") is False]
        suspect = [
            take
            for take in recorded
            if take.get("operator_valid") is not False
            and (take.get("quick_qc") or {}).get("state") == "suspect"
        ]
        valid = [
            take
            for take in completed
            if take.get("operator_valid") is not False
            and take.get("state") == "complete"
        ]
        return {
            "recorded_takes": len(recorded),
            "completed_takes": len(completed),
            "valid_takes": len(valid),
            "rejected_takes": len(rejected),
            "suspect_takes": len(suspect),
            "next_take_id": self._next_take_id_from(takes_root or self.takes_root),
        }

    def task_status(self) -> Dict[str, object]:
        with self._lock:
            if not self._active:
                return {
                    "active": False,
                    "task": None,
                    "session_id": None,
                    "stats": self._take_stats(self.list_takes()),
                }
            takes = self.list_takes()
            task = {
                "task_id": self.task_id,
                "name": self.task,
                "speech_name": self.task_speech_name,
                "instruction": self.task_instruction,
                "capture_profile": self.capture_profile,
                "station_check_after_take": self.station_check_after_take,
                "recording_subdirectory": self.recording_subdirectory(),
            }
            return {
                "active": True,
                "task": task,
                "session_id": self.session_id,
                "stats": self._take_stats(takes),
            }
