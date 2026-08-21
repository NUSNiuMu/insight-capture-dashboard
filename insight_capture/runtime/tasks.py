"""Configured capture tasks and their operator-facing metadata."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional


TASK_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


@dataclass(frozen=True)
class CaptureTask:
    task_id: str
    name: str
    speech_name: str
    instruction: str
    capture_profile: str
    voice_aliases: tuple[str, ...]
    station_check_after_take: bool

    def as_dict(self) -> Dict[str, object]:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "speech_name": self.speech_name,
            "instruction": self.instruction,
            "capture_profile": self.capture_profile,
            "voice_aliases": list(self.voice_aliases),
            "station_check_after_take": self.station_check_after_take,
        }

    def as_config_dict(self) -> Dict[str, object]:
        return {
            "id": self.task_id,
            "name": self.name,
            "speech_name": self.speech_name,
            "instruction": self.instruction,
            "capture_profile": self.capture_profile,
            "voice_aliases": list(self.voice_aliases),
            "station_check_after_take": self.station_check_after_take,
        }


class CaptureTaskCatalog:
    """Read the small tracked registry used by UI, API, and voice workflows."""

    def __init__(
        self,
        tasks: Iterable[CaptureTask],
        *,
        default_task_id: Optional[str] = None,
        managed_root: Optional[Path] = None,
    ) -> None:
        self._lock = threading.RLock()
        self._tasks = {task.task_id: task for task in tasks}
        self.managed_root = Path(managed_root).resolve() if managed_root else None
        self.default_task_id = str(default_task_id or "").strip() or None
        if self.default_task_id and self.default_task_id not in self._tasks:
            raise ValueError(
                f"Default capture task is not configured: {self.default_task_id}"
            )

    @classmethod
    def load(
        cls, path: Path, *, managed_root: Optional[Path] = None
    ) -> "CaptureTaskCatalog":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        raw_tasks = payload.get("tasks") if isinstance(payload, dict) else None
        if not isinstance(raw_tasks, list):
            raise ValueError("capture_tasks.json must contain a tasks list")
        tasks = {}
        for raw in raw_tasks:
            task = cls._task_from_payload(raw)
            tasks[task.task_id] = task
        resolved_managed_root = (
            Path(managed_root).resolve() if managed_root is not None else None
        )
        if resolved_managed_root is not None and resolved_managed_root.is_dir():
            for task_path in sorted(resolved_managed_root.glob("*/task.json")):
                managed_payload = json.loads(task_path.read_text(encoding="utf-8"))
                task = cls._task_from_payload(managed_payload)
                if task_path.parent.name != task.task_id:
                    raise ValueError(
                        f"Managed task folder does not match task id: {task_path}"
                    )
                tasks[task.task_id] = task
        return cls(
            tasks.values(),
            default_task_id=payload.get("default_task_id"),
            managed_root=resolved_managed_root,
        )

    @staticmethod
    def _task_from_payload(raw: Mapping[str, object]) -> CaptureTask:
        if not isinstance(raw, Mapping):
            raise ValueError("Each capture task must be an object")
        task_id = str(raw.get("id") or raw.get("task_id") or "").strip()
        if not TASK_ID_PATTERN.fullmatch(task_id):
            raise ValueError(
                "Task ID must use 1-64 lowercase letters, numbers, '_' or '-'."
            )
        name = str(raw.get("name") or "").strip()
        instruction = str(raw.get("instruction") or "").strip()
        if not name or not instruction:
            raise ValueError("Capture tasks require name and instruction")
        raw_aliases = raw.get("voice_aliases", [])
        if not isinstance(raw_aliases, (list, tuple)):
            raise ValueError("voice_aliases must be a list")
        aliases = tuple(
            str(alias).strip() for alias in raw_aliases if str(alias).strip()
        )
        return CaptureTask(
            task_id=task_id,
            name=name,
            speech_name=str(raw.get("speech_name") or name).strip(),
            instruction=instruction,
            capture_profile=str(raw.get("capture_profile") or "").strip(),
            voice_aliases=aliases,
            station_check_after_take=bool(raw.get("station_check_after_take", False)),
        )

    def _persist(self, task: CaptureTask) -> None:
        if self.managed_root is None:
            raise RuntimeError("Managed task storage is not configured.")
        root = self.managed_root / task.task_id
        root.mkdir(parents=True, exist_ok=True)
        path = root / "task.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(task.as_config_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def create(self, payload: Mapping[str, object]) -> CaptureTask:
        task = self._task_from_payload(payload)
        with self._lock:
            if task.task_id in self._tasks:
                raise ValueError(f"Task already exists: {task.task_id}")
            self._persist(task)
            self._tasks[task.task_id] = task
            return task

    def update(self, task_id: str, payload: Mapping[str, object]) -> CaptureTask:
        normalized_id = str(task_id or "").strip()
        with self._lock:
            current = self.get(normalized_id)
            requested_id = str(
                payload.get("id") or payload.get("task_id") or normalized_id
            ).strip()
            if requested_id != normalized_id:
                raise ValueError("Task ID cannot be changed after creation.")
            merged = {**current.as_config_dict(), **dict(payload), "id": normalized_id}
            task = self._task_from_payload(merged)
            self._persist(task)
            self._tasks[normalized_id] = task
            return task

    def get(self, task_id: str) -> CaptureTask:
        with self._lock:
            try:
                return self._tasks[str(task_id).strip()]
            except KeyError as exc:
                raise ValueError(f"Unknown capture task: {task_id}") from exc

    def default(self) -> Optional[CaptureTask]:
        return self.get(self.default_task_id) if self.default_task_id else None

    def list(self) -> list[Dict[str, object]]:
        with self._lock:
            return [task.as_dict() for task in self._tasks.values()]
