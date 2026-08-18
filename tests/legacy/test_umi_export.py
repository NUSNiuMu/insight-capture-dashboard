from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from insight_capture.web.umi_export import UmiExportManager  # noqa: E402


def test_rejects_task_specific_episode_modes(tmp_path: Path) -> None:
    manager = UmiExportManager(tmp_path / "project", tmp_path / "bags")
    with pytest.raises(ValueError, match="bag or auto_pause"):
        manager.start([], episode_mode="task_specific")
