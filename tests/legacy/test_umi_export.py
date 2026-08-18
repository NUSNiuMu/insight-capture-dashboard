from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from insight_capture.services.dataset_export import UmiExportManager  # noqa: E402
import insight_capture.services.dataset_export as dataset_export  # noqa: E402


def test_rejects_task_specific_episode_modes(tmp_path: Path) -> None:
    manager = UmiExportManager(tmp_path / "project", tmp_path / "bags")
    with pytest.raises(ValueError, match="bag or auto_pause"):
        manager.start([], episode_mode="task_specific")


def test_ego_route_launches_the_module_entry_point(tmp_path: Path) -> None:
    manager = UmiExportManager(tmp_path / "project", tmp_path / "bags")
    item = SimpleNamespace(
        bag_path=tmp_path / "bags" / "episode",
        dataset_name="episode_lerobot",
        export_format="lerobot",
        output_path=tmp_path / "project" / "outputs" / "lerobot_datasets" / "episode",
        route="pending",
        route_diagnostics=None,
    )
    item.bag_path.mkdir(parents=True)
    job = SimpleNamespace(
        camera_names=["insight9_a"],
        stage="starting",
        task="fold cloth",
        image_size=None,
        episode_mode="bag",
    )
    commands = []

    class FailedProcess:
        stdout = ()

        @staticmethod
        def wait() -> int:
            return 1

    def start_process(command, **_kwargs):
        commands.append(command)
        return FailedProcess()

    with (
        patch.object(
            dataset_export,
            "load_camera_specs",
            return_value=[SimpleNamespace(name="insight9_a", role="head")],
        ),
        patch.object(
            dataset_export,
            "inspect_gripper_markers",
            return_value={"route": "ego_hand"},
        ),
        patch.object(dataset_export, "build_ego_spec", return_value={}),
        patch.object(dataset_export.subprocess, "Popen", side_effect=start_process),
        pytest.raises(RuntimeError, match="Dataset exporter failed"),
    ):
        manager._export_item(job, item, 0)

    assert commands[0][:4] == [
        sys.executable,
        "-u",
        "-m",
        manager._EGO_LEROBOT_MODULE,
    ]
