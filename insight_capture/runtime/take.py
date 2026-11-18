"""Durable Session/Take metadata for field capture."""

from __future__ import annotations

import json
import re
import os
import shutil
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Dict, Mapping, Optional

from insight_capture.core.bag import recording_timing
from .tasks import CaptureTask, CaptureTaskCatalog
from .recovered_take import recovered_take_info


def _with_recording_timing(payload: Dict) -> Dict:
    """Enrich historical takes on read without rewriting their lifecycle dates."""
    if not isinstance(payload, dict):
        raise ValueError("Take metadata must be an object.")
    payload.pop('recovery', None)
    recovery = recovered_take_info(payload)
    if recovery is not None:
        payload['recovery'] = recovery
    bag_path = payload.get("bag_path")
    if isinstance(bag_path, str) and bag_path and payload.get("state") in {
        "complete", "saved", "finalization_failed"
    }:
        saved = recording_timing(Path(bag_path))
        if saved is not None:
            payload["recording_timing"] = saved
    return payload


def _slug(value: object, fallback: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z_-]+", "_", str(value or "").strip())
    return normalized.strip("_") or fallback


class SessionTakeStore:
    """Persist task metadata and remove owned data on explicit task deletion."""

    def __init__(
        self,
        results_root: Path,
        config: Optional[Dict] = None,
        *,
        task_catalog: CaptureTaskCatalog,
    ) -> None:
        self._lock = threading.RLock()
        self._stats_cache: Dict[Path, tuple[int, float, Dict[str, int]]] = {}
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
        self.capture_profile = ""
        self.operator = "unknown"
        self.session_id = ""
        self.root = self.sessions_root
        self.takes_root = self.sessions_root / "takes"
        self.operator = str((config or {}).get("operator") or "unknown")
        self._restore_task_session()

    def _configure_task_session(self, task: CaptureTask, session_id: str) -> None:
        self._active = True
        self.task_id = task.task_id
        self.task = task.name
        self.task_instruction = task.instruction
        self.capture_profile = task.capture_profile
        self.session_id = session_id
        self.root = self.sessions_root / session_id
        self.takes_root = self.root / "takes"
        self.takes_root.mkdir(parents=True, exist_ok=True)
        self._current_take_id = None

    @staticmethod
    def _task_set_id(task_id: str) -> str:
        return _slug(task_id, "task")

    def _restore_task_session(self) -> None:
        state = None
        if self._active_state_path.is_file():
            try:
                state = json.loads(self._active_state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = None
        if isinstance(state, dict) and state.get("active"):
            task = self._task_catalog.get(str(state.get("task_id") or ""))
            self._configure_task_session(task, self._task_set_id(task.task_id))
            self._write_session()
            self._write_active_state()
            return
        if isinstance(state, dict):
            task_id = str(state.get("task_id") or "").strip()
            if task_id:
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
        self._stats_cache.pop(path.parent, None)

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
            "next_take_id": self._next_take_id(),
            "task": self.task,
            "task_id": self.task_id,
            "task_name": self.task,
            "task_instruction": self.task_instruction,
            "capture_profile": self.capture_profile,
            "operator": self.operator,
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
        with self._lock:
            task = self._task_catalog.create(payload)
            return task.as_dict()

    def update_task(
        self, task_id: str, payload: Mapping[str, object]
    ) -> Dict[str, object]:
        with self._lock:
            current = self.current()
            if current and current.get("state") in {"starting", "recording", "finalizing"}:
                raise RuntimeError("Cannot edit a task while a take is active.")
            task = self._task_catalog.update(task_id, payload)
            if self.task_id == task.task_id:
                self.task = task.name
                self.task_instruction = task.instruction
                self.capture_profile = task.capture_profile
                self._write_session()
                self._write_active_state()
            return task.as_dict()

    def end_task(self, expected_task_id: Optional[str] = None) -> Dict[str, object]:
        with self._lock:
            if expected_task_id is not None and self.task_id != expected_task_id:
                raise RuntimeError("当前任务已变化，请重新打开任务列表后操作。")
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

    @staticmethod
    def _validate_removal_path(target: Path, root: Path, *, allow_file=False) -> None:
        resolved = target.resolve()
        if resolved == root or not target.is_relative_to(root) or not resolved.is_relative_to(root):
            raise RuntimeError("录制目录不在采集存储内，不能删除。")
        for path in [target, *target.parents]:
            if path == root:
                break
            if path.is_symlink():
                raise RuntimeError("录制目录含软链接，不能删除。")
        if not target.exists():
            return
        if allow_file and target.is_file():
            return
        if not target.is_dir():
            raise RuntimeError("录制目录异常，不能删除。")
        for directory, dirs, _ in os.walk(target, followlinks=False):
            if Path(directory).is_mount() or any((Path(directory) / child).is_mount() for child in dirs):
                raise RuntimeError("录制目录包含挂载点，不能删除。")

    def _take_data_paths(self, payload: Dict, raw_roots, *, folder=None) -> list[tuple[Path, Path]]:
        roots = [Path(root).resolve() for root in raw_roots]
        paths = []
        bag_name = payload["bag_name"]
        if folder is None:
            folder = self._task_set_id(payload["task_id"])
        if payload.get("bag_path"):
            bag = Path(payload["bag_path"])
            root = next((root for root in roots if bag.is_relative_to(root) and bag != root), None)
            if root is None or bag.name != bag_name:
                raise RuntimeError("录制目录与记录不匹配，不能删除。")
            paths.append((bag, root))
            # Recovery copies belong to this Take only when their manifest names
            # its exact archived source. A similarly named ordinary bag is kept.
            recovered = bag.with_name(bag.name + "_recovered")
            manifest_path = recovered / "recording_manifest.json"
            if manifest_path.is_file():
                manifest = json.loads(manifest_path.read_text())
                archive = root / "_staging" / ("." + bag_name + ".original")
                if (manifest.get("recovered") is True and manifest.get("bag_name") == recovered.name
                        and Path(manifest.get("recovery_source", "")) == archive):
                    paths.extend([(recovered, root), (archive, root)])
        for root in roots:
            staging = root / "_staging" / bag_name
            marker = staging / ".recording_target.json"
            if marker.is_file() and json.loads(marker.read_text()).get("output_subdirectory") == folder:
                paths.append((staging, root))
        return list(dict.fromkeys(paths))

    def _distributed_data_paths(self, payload: Dict, raw_roots) -> list[tuple[Path, Path]]:
        distributed = payload.get("distributed") or {}
        if not distributed:
            return []
        sid = distributed.get("session_id", "")
        if (not isinstance(sid, str) or not re.fullmatch(r"[0-9a-f]{32}", sid)
                or distributed.get("task_id") != payload["task_id"]
                or distributed.get("take_id") != payload["take_id"]):
            raise RuntimeError("双端录制身份与记录不匹配，不能删除。")
        jetson = Path(distributed.get("jetson_path") or "")
        root = next((Path(root).resolve() for root in raw_roots
                     if jetson == Path(root).resolve() / "_distributed" / (sid + "_jetson")), None)
        if root is None:
            raise RuntimeError("双端原始录制目录与记录不匹配，不能删除。")
        base = root / "_distributed"
        manifest = base / (sid + ".json")
        ego_copy = base / (sid + "_ego")
        self._validate_removal_path(manifest, root, allow_file=True)
        if manifest.exists():
            session = json.loads(manifest.read_text())
            expected = {
                "session_id": sid, "task_id": payload["task_id"], "take_id": payload["take_id"],
                "output_path": payload.get("bag_path"), "jetson_path": str(jetson),
                "ego_copy_path": str(ego_copy), "manifest_path": str(manifest),
            }
            if any(session.get(key) != value for key, value in expected.items()):
                raise RuntimeError("双端录制清单与记录不匹配，不能删除。")
        # The coordinator always records wrists under this fixed session name.
        # Reuse bag ownership checks for interrupted/staged wrist recordings.
        paths = self._take_data_paths({"task_id": "_distributed", "bag_name": jetson.name,
                                       "bag_path": str(jetson)}, [root], folder="_distributed")
        return [*paths, (ego_copy, root), (manifest, root)]

    def _validate_data_paths(self, paths, protected_paths=()) -> None:
        for target, root in paths:
            manifest = target.parent == root / "_distributed" and re.fullmatch(r"[0-9a-f]{32}\.json", target.name)
            self._validate_removal_path(target, root, allow_file=bool(manifest))
            if any(Path(protected).resolve().is_relative_to(target.resolve()) for protected in protected_paths):
                raise RuntimeError("请先停止该条录制的回放。")

    @staticmethod
    def _remove_data_paths(paths) -> None:
        # Callers place raw data before manifests and task metadata, preserving
        # ownership records for retry when a data removal fails.
        for target, _ in paths:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.is_file():
                target.unlink()

    def delete_take(self, task_id: str, take_id: int, expected_bag_name: str,
                    raw_roots, *, protected_paths=()) -> Dict:
        with self._lock:
            self._task_catalog.get(task_id)
            current = self.current()
            if current and current.get("state") in {"starting", "recording", "finalizing"}:
                raise RuntimeError("正在准备、录制或保存，不能删除录制。")
            takes_root = self.sessions_root / self._task_set_id(task_id) / "takes"
            metadata = takes_root / f"take_{take_id:04d}.json"
            if metadata.is_symlink() or takes_root.is_symlink() or takes_root.parent.is_symlink():
                raise RuntimeError("录制记录目录异常，不能删除。")
            payload = json.loads(metadata.read_text())
            if (payload.get("task_id") != task_id or payload.get("take_id") != take_id
                    or payload.get("bag_name") != expected_bag_name):
                raise RuntimeError("录制记录已变化，请关闭并重新确认删除。")
            if payload.get("state") in {"reserved", "starting", "recording", "finalizing"}:
                raise RuntimeError("正在准备、录制或保存，不能删除录制。")
            paths = self._take_data_paths(payload, raw_roots) + self._distributed_data_paths(payload, raw_roots)
            self._validate_data_paths(paths, protected_paths)
            owned = {target.resolve() for target, _ in paths}
            for other in self._list_takes_from(takes_root, include_timing=False):
                if other.get("take_id") == take_id:
                    continue
                shared_bag = other.get("bag_path") and Path(other["bag_path"]).resolve() in owned
                sid = (payload.get("distributed") or {}).get("session_id")
                shared_session = sid and (other.get("distributed") or {}).get("session_id") == sid
                if shared_bag or shared_session:
                    raise RuntimeError("数据目录与另一条录制共用，不能删除。")
            # Persist the next number before removing the highest Take. It must
            # never be reused, including after task switches or an app restart.
            session_path = takes_root.parent / "session.json"
            session = json.loads(session_path.read_text()) if session_path.is_file() else {}
            session["next_take_id"] = self._next_take_id_from(takes_root)
            self._write_json(session_path, session)
            self._remove_data_paths(paths)
            metadata.unlink()
            if self.task_id == task_id and self._current_take_id == take_id:
                self._current_take_id = None
            self._stats_cache.pop(takes_root, None)
            return {"task_id": task_id, "take_id": take_id, "paths": [str(target) for target, _ in paths],
                    "distributed_session_ids": [payload["distributed"]["session_id"]] if payload.get("distributed") else []}

    def delete_task(self, task_id: str, raw_roots, expected_recorded_takes: int) -> Dict:
        with self._lock:
            task = self._task_catalog.get(task_id)
            current = self.current()
            if current and current.get("state") in {"starting", "recording", "finalizing"}:
                raise RuntimeError("正在准备、录制或保存，不能删除任务。")
            folder = self._task_set_id(task_id)
            if any(item["task_id"] != task_id and self._task_set_id(item["task_id"]) == folder
                   for item in self._task_catalog.list()):
                raise RuntimeError("任务目录与另一任务重名，不能删除。")
            takes_root = self.sessions_root / folder / "takes"
            self._stats_cache.pop(takes_root, None)
            count = self._cached_take_stats(takes_root)["recorded_takes"]
            if count != expected_recorded_takes:
                raise RuntimeError("录制条数已变化，请关闭并重新确认删除。")
            takes = self._list_takes_from(takes_root, include_timing=False)
            paths = []
            for take in takes:
                if take.get("task_id") != task_id:
                    raise RuntimeError("录制记录与任务不匹配，不能删除。")
                paths.extend(self._take_data_paths(take, raw_roots))
                paths.extend(self._distributed_data_paths(take, raw_roots))
            # A failed start can leave a staging marker without Take metadata.
            for raw_root in raw_roots:
                root = Path(raw_root).resolve()
                for marker in (root / "_staging").glob("*/.recording_target.json"):
                    try:
                        target = json.loads(marker.read_text())
                    except (OSError, ValueError):
                        # Unknown ownership must be preserved, not block cleanup
                        # of an unrelated task.
                        continue
                    if isinstance(target, dict) and target.get("output_subdirectory") == folder:
                        paths.append((marker.parent, root))
            for root, name in [(Path(root), folder) for root in raw_roots] + [
                (self.sessions_root, folder),
                *([(self._task_catalog.managed_root, task_id)] if self._task_catalog.managed_root else []),
            ]:
                root = root.resolve()
                target = root / name
                paths.append((target, root))
            paths = list(dict.fromkeys(paths))
            self._validate_data_paths(paths)
            # All paths are validated before any removal. Raw data is removed
            # before its catalog entry, so an I/O failure remains visible for retry.
            self._remove_data_paths(paths)
            if self.task_id == task_id:
                self._active = False
                self.task_id = self.session_id = self.task_instruction = self.capture_profile = ""
                self.task = "unspecified"
                self._current_take_id = None
                self.root = self.sessions_root
                self.takes_root = self.sessions_root / "takes"
                self._write_active_state()
            self._task_catalog.remove(task_id)
            self._stats_cache.clear()
            return {"task_id": task_id, "name": task.name, "recorded_takes": count,
                    "distributed_session_ids": [take["distributed"]["session_id"] for take in takes if take.get("distributed")]}

    def list_tasks(self) -> list[Dict[str, object]]:
        tasks = []
        for item in self._task_catalog.list():
            task_set_id = self._task_set_id(str(item.get("task_id") or ""))
            takes_root = self.sessions_root / task_set_id / "takes"
            tasks.append({
                **item,
                "task_set_id": task_set_id,
                "recording_subdirectory": task_set_id,
                "active": self._active and self.task_id == item.get("task_id"),
                "stats": self._cached_take_stats(takes_root),
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
        next_id = max(ids, default=0) + 1
        try:
            saved = json.loads((takes_root.parent / "session.json").read_text()).get("next_take_id")
            if type(saved) is int:
                next_id = max(next_id, saved)
        except (OSError, ValueError, AttributeError):
            pass
        return next_id

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
                "operator_valid": None,
                "operator_decision": "pending",
                "reject_reason": None,
                "anomaly_timeline": [],
            }
            self._write_json(self._path(take_id), payload)
            self._current_take_id = take_id
            return deepcopy(payload)

    def mark_recording(self, output_path: object, recording_status: Optional[Dict] = None) -> Dict:
        with self._lock:
            payload = self.current()
            if payload is None:
                raise RuntimeError("No take is reserved.")
            payload["state"] = "recording"
            payload["bag_path"] = str(output_path or "") or None
            payload["recording_timing"] = deepcopy((recording_status or {}).get("recording_timing"))
            payload["require_global_pose"] = (recording_status or {}).get("require_global_pose", True)
            if (recording_status or {}).get("distributed"):
                payload["distributed"] = recording_status["distributed"]
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
            return self._complete(payload, recording_status, self._path(payload["take_id"]))

    def complete_distributed(self, recording_status: Dict) -> Optional[Dict]:
        distributed = recording_status["distributed"]
        with self._lock:
            path = (self.sessions_root / self._task_set_id(distributed["task_id"]) / "takes"
                    / f"take_{int(distributed['take_id']):04d}.json")
            if not path.is_file():
                return None
            payload = json.loads(path.read_text())
            if (payload.get("distributed") or {}).get("session_id") != distributed["session_id"]:
                raise RuntimeError("双端录制编号与任务条目不匹配")
            return self._complete(payload, recording_status, path)

    def _complete(self, payload: Dict, recording_status: Dict, path: Path) -> Dict:
        reasons = []
        if recording_status.get("merge_state") == "error":
            reasons.append(str(recording_status.get("merge_error") or "recording finalization failed"))
        network = recording_status.get("network_audit") or {}
        if isinstance(network, dict) and network.get("ok") is False:
            reasons.append("recording network audit failed")
        image_audit = recording_status.get("image_header_audit") or {}
        distributed = recording_status.get("distributed") or {}
        if distributed.get("clock_violations"):
            reasons.append("distributed clock synchronization audit failed")
        reasons.extend(distributed.get("quality_issues") or [])
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
        qc_state = "pending" if recording_status.get("merge_state") == "saved" else "fail" if recording_status.get("merge_state") == "error" else (
            "suspect" if reasons or quality_anomalies else "pass"
        )
        payload.update({
            "state": "saved" if recording_status.get("merge_state") == "saved" else "finalizing" if recording_status.get("merge_state") in {"merging", "finalizing"} else "complete" if qc_state != "fail" else "finalization_failed",
            "end_epoch_s": payload.get("end_epoch_s") or time.time(),
            "bag_path": str(recording_status.get("output_path") or payload.get("bag_path") or "") or None,
            "quick_qc": {
                "state": qc_state,
                "reasons": reasons,
                "image_header_audit": image_audit or None,
                "network_audit": network or None,
            },
            "distributed": distributed or None,
        })
        if recording_status.get("recording_timing"):
            payload["recording_timing"] = deepcopy(recording_status["recording_timing"])
        if recording_status.get("recorder_failure"):
            payload["recorder_failure"] = deepcopy(recording_status["recorder_failure"])
        self._write_json(path, payload)
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

    def reject_current(
        self, reason: object = "operator_rejected", *,
        expected_task_id: Optional[str] = None, expected_take_id: Optional[int] = None,
    ) -> Dict:
        """Mark a take invalid; raw bag data is deliberately never deleted."""
        with self._lock:
            payload = self.current()
            if payload is None:
                raise RuntimeError("No take is available to reject.")
            if ((expected_task_id is not None and self.task_id != expected_task_id)
                    or (expected_take_id is not None and payload["take_id"] != expected_take_id)):
                raise RuntimeError("录次已变化，作废请求未执行，请刷新状态。")
            reason = str(reason or "operator_rejected")
            if (payload.get("operator_valid") is False
                    and payload.get("operator_decision") == "rejected"
                    and payload.get("reject_reason") == reason):
                return deepcopy(payload)
            payload.update({
                "operator_valid": False,
                "operator_decision": "rejected",
                "reject_reason": reason,
                "operator_decided_at_epoch_s": time.time(),
            })
            self._write_json(self._path(payload["take_id"]), payload)
            return deepcopy(payload)

    def current(self) -> Optional[Dict]:
        with self._lock:
            if not self._active:
                return None
            if self._current_take_id is None:
                paths = self._take_paths(self.takes_root)
                if not paths:
                    return None
                try:
                    self._current_take_id = int(paths[-1].stem.split("_")[-1])
                except ValueError:
                    return None
            try:
                return _with_recording_timing(deepcopy(self._read(self._current_take_id)))
            except (OSError, ValueError):
                return None

    def task_recordings(self, task_id: str) -> Dict:
        """Browse a catalog task without activating it or touching its files."""
        with self._lock:
            task = self._task_catalog.get(task_id)
            root = self.sessions_root / self._task_set_id(task.task_id) / "takes"
            return {"task": task.as_dict(), "takes": self._list_takes_from(root)}

    def recovered_file(self, task_id: str, take_id: int, kind: str) -> Path:
        """Resolve only a known recovered Take's three published files."""
        with self._lock:
            task = self._task_catalog.get(task_id)
            if take_id < 1 or kind not in {'data', 'metadata', 'manifest'}:
                raise FileNotFoundError('Recovery file not found')
            root = self.sessions_root / self._task_set_id(task.task_id) / 'takes'
            payload = json.loads((root / f'take_{take_id:04d}.json').read_text())
            if (not isinstance(payload, dict) or payload.get('task_id') != task_id or
                    payload.get('take_id') != take_id):
                raise FileNotFoundError('Recovery Take not found')
            info = recovered_take_info(payload)
            if info is None:
                raise FileNotFoundError('Recovery file not available')
            row = next(row for row in info['files'] if row['kind'] == kind)
            return Path(info['bag_path']) / row['name']

    def list_takes(self) -> list[Dict]:
        return self._list_takes_from(self.takes_root)

    @staticmethod
    def _take_paths(root: Path) -> list[Path]:
        return sorted(
            (path for path in root.glob("take_*.json") if re.fullmatch(r"take_[0-9]+", path.stem)),
            key=lambda path: int(path.stem[5:]),
        )

    @staticmethod
    def _list_takes_from(takes_root: Path, *, include_timing: bool = True) -> list[Dict]:
        items = []
        for path in reversed(SessionTakeStore._take_paths(takes_root)):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    continue
                items.append(_with_recording_timing(payload) if include_timing else payload)
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

    def _cached_take_stats(self, takes_root: Path) -> Dict[str, int]:
        """Cache summaries with local-write invalidation and external-edit refresh."""
        with self._lock:
            try:
                signature = takes_root.stat().st_mtime_ns
            except FileNotFoundError:
                signature = -1
            now = time.monotonic()
            cached = self._stats_cache.get(takes_root)
            # TTL covers external edits and coalesced filesystem timestamps.
            if cached is None or cached[0] != signature or now >= cached[1]:
                stats = self._take_stats(self._list_takes_from(takes_root, include_timing=False), takes_root=takes_root)
                cached = (signature, now + 30.0, stats)
                self._stats_cache[takes_root] = cached
            return dict(cached[2])

    def task_status(self) -> Dict[str, object]:
        with self._lock:
            if not self._active:
                return {
                    "active": False,
                    "task": None,
                    "last_task_id": self.task_id,
                    "session_id": None,
                    "stats": self._cached_take_stats(self.takes_root),
                }
            task = {
                "task_id": self.task_id,
                "name": self.task,
                "instruction": self.task_instruction,
                "capture_profile": self.capture_profile,
                "recording_subdirectory": self.recording_subdirectory(),
            }
            return {
                "active": True,
                "task": task,
                "session_id": self.session_id,
                "stats": self._cached_take_stats(self.takes_root),
            }
