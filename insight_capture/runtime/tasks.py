"""Configured capture tasks and their operator-facing metadata."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional


@dataclass(frozen=True)
class CaptureTask:
    task_id: str
    name: str
    instruction: str
    capture_profile: str
    voice_aliases: tuple[str, ...]
    station_check_after_take: bool

    def as_dict(self) -> Dict[str, object]:
        return {
            "task_id": self.task_id,
            "name": self.name,
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
    ) -> None:
        self._tasks = {task.task_id: task for task in tasks}
        self.default_task_id = str(default_task_id or "").strip() or None
        if self.default_task_id and self.default_task_id not in self._tasks:
            raise ValueError(
                f"Default capture task is not configured: {self.default_task_id}"
            )

    @classmethod
    def load(cls, path: Path) -> "CaptureTaskCatalog":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        raw_tasks = payload.get("tasks") if isinstance(payload, dict) else None
        if not isinstance(raw_tasks, list):
            raise ValueError("capture_tasks.json must contain a tasks list")
        tasks = []
        for raw in raw_tasks:
            if not isinstance(raw, dict):
                raise ValueError("Each capture task must be an object")
            task_id = str(raw.get("id") or "").strip()
            name = str(raw.get("name") or "").strip()
            instruction = str(raw.get("instruction") or "").strip()
            if not task_id or not name or not instruction:
                raise ValueError("Capture tasks require id, name, and instruction")
            aliases = tuple(
                str(alias).strip()
                for alias in raw.get("voice_aliases", [])
                if str(alias).strip()
            )
            tasks.append(
                CaptureTask(
                    task_id=task_id,
                    name=name,
                    instruction=instruction,
                    capture_profile=str(raw.get("capture_profile") or "").strip(),
                    voice_aliases=aliases,
                    station_check_after_take=bool(
                        raw.get("station_check_after_take", False)
                    ),
                )
            )
        return cls(tasks, default_task_id=payload.get("default_task_id"))

    def get(self, task_id: str) -> CaptureTask:
        try:
            return self._tasks[str(task_id).strip()]
        except KeyError as exc:
            raise ValueError(f"Unknown capture task: {task_id}") from exc

    def default(self) -> Optional[CaptureTask]:
        return self.get(self.default_task_id) if self.default_task_id else None

    def list(self) -> list[Dict[str, object]]:
        return [task.as_dict() for task in self._tasks.values()]
