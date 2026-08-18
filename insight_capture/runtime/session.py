"""Session identity and operator metadata."""

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional


@dataclass(frozen=True)
class SessionMetadata:
    session_id: str
    task: str
    operator: str
    station_check_after_take: bool

    @classmethod
    def from_config(cls, config: Optional[Dict], slug) -> "SessionMetadata":
        settings = dict(config or {})
        today = datetime.now().strftime("%Y%m%d")
        return cls(
            session_id=slug(settings.get("session_id"), f"{today}-default"),
            task=str(settings.get("task") or "unspecified"),
            operator=str(settings.get("operator") or "unknown"),
            station_check_after_take=bool(settings.get("station_check_after_take", False)),
        )
