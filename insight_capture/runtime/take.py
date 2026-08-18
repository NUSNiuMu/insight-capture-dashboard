"""Durable Session/Take metadata for field capture."""

from __future__ import annotations

import json
import re
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Dict, Optional

from .session import SessionMetadata


def _slug(value: object, fallback: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z_-]+", "_", str(value or "").strip())
    return normalized.strip("_") or fallback


class SessionTakeStore:
    """Persist operator decisions and lightweight QC without touching raw bags."""

    def __init__(self, results_root: Path, config: Optional[Dict] = None) -> None:
        metadata = SessionMetadata.from_config(config, _slug)
        self.task = metadata.task
        self.operator = metadata.operator
        self.session_id = metadata.session_id
        self.station_check_after_take = metadata.station_check_after_take
        self.root = Path(results_root).resolve() / "sessions" / self.session_id
        self.takes_root = self.root / "takes"
        self.takes_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._current_take_id: Optional[int] = None
        self._write_session()

    def _write_json(self, path: Path, payload: Dict) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(path)

    def _write_session(self) -> None:
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
            "operator": self.operator,
            "station_check_after_take": self.station_check_after_take,
            "created_at_epoch_s": created,
            "updated_at_epoch_s": time.time(),
        })

    def _path(self, take_id: int) -> Path:
        return self.takes_root / f"take_{int(take_id):04d}.json"

    def _read(self, take_id: int) -> Dict:
        return json.loads(self._path(take_id).read_text(encoding="utf-8"))

    def _next_take_id(self) -> int:
        ids = []
        for path in self.takes_root.glob("take_*.json"):
            match = re.fullmatch(r"take_(\d+)\.json", path.name)
            if match:
                ids.append(int(match.group(1)))
        return max(ids, default=0) + 1

    def reserve_take(
        self, requested_bag_name: Optional[str] = None, *, trigger: str = "web"
    ) -> Dict:
        with self._lock:
            if self._current_take_id is not None:
                current = self._read(self._current_take_id)
                if current.get("state") in {"starting", "recording", "finalizing"}:
                    raise RuntimeError("A take is already active.")
            take_id = self._next_take_id()
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            bag_name = _slug(
                requested_bag_name,
                f"{_slug(self.task, 'task')}_take_{take_id:04d}_{timestamp}",
            )
            payload = {
                "schema_version": 1,
                "session_id": self.session_id,
                "task": self.task,
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
            if isinstance(image_audit, dict) and image_audit.get("ok") is False:
                reasons.append("image continuity audit failed")
            anomalies = payload.get("anomaly_timeline") or []
            if any(item.get("severity") == "critical" for item in anomalies):
                reasons.append("critical live QC anomaly")
            qc_state = "fail" if recording_status.get("merge_state") == "error" else (
                "suspect" if reasons or anomalies else "pass"
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
        items = []
        for path in sorted(self.takes_root.glob("take_*.json"), reverse=True):
            try:
                items.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return items

    def snapshot(self) -> Dict:
        return {
            "session": {
                "session_id": self.session_id,
                "task": self.task,
                "operator": self.operator,
                "station_check_after_take": self.station_check_after_take,
            },
            "current_take": self.current(),
            "takes": self.list_takes(),
        }
