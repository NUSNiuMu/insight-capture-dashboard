"""Explicit dependencies shared by dashboard HTTP services."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from handpose import HandPoseManager
from post_processing import (
    OptimizationManager,
    PreparedPlaybackManager,
    RecordingManager,
)

from .gripper_extraction import GripperExtractionManager
from .scoring import ScoringManager
from .umi_export import UmiExportManager


@dataclass
class DashboardContext:
    node: Any
    web_root: Optional[Path]
    project_root: Path
    recording_manager: RecordingManager
    results_root: Path
    scoring_manager: ScoringManager = field(init=False)
    prepared_playback_manager: PreparedPlaybackManager = field(init=False)
    optimization_manager: OptimizationManager = field(init=False)
    handpose_manager: HandPoseManager = field(init=False)
    gripper_extraction_manager: GripperExtractionManager = field(init=False)
    umi_export_manager: UmiExportManager = field(init=False)
    _image_capabilities_cache: Optional[Dict[str, object]] = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.web_root = self.web_root.resolve() if self.web_root else None
        self.project_root = self.project_root.resolve()
        self.results_root = self.results_root.resolve()
        self.scoring_manager = ScoringManager(
            rosbag_root=self.recording_manager.rosbag_root,
            results_root=self.results_root,
        )
        self.prepared_playback_manager = PreparedPlaybackManager(
            rosbag_root=self.recording_manager.rosbag_root,
            cache_root=self.results_root / "playback_cache",
        )
        pipeline_script = (
            self.project_root.parent
            / "looper-vio-colmap-handoff"
            / "scripts"
            / "run_pipeline_from_rosbag.py"
        )
        self.optimization_manager = OptimizationManager(
            project_root=self.project_root,
            pipeline_script=pipeline_script,
        )
        self.handpose_manager = HandPoseManager(
            project_root=self.project_root,
            rosbag_root=self.recording_manager.rosbag_root,
        )
        self.gripper_extraction_manager = GripperExtractionManager(
            project_root=self.project_root,
            rosbag_root=self.recording_manager.rosbag_root,
        )
        self.umi_export_manager = UmiExportManager(
            project_root=self.project_root,
            rosbag_root=self.recording_manager.rosbag_root,
        )
