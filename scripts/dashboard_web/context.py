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
        self.prepared_playback_manager.configure_background(
            self.recording_manager,
            self.playback_configuration,
        )
        self.recording_manager.add_recording_completed_callback(
            lambda path: self.prepared_playback_manager.enqueue(path.name)
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

    def playback_configuration(
        self,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        """Return the stable camera and pose identity used by review bundles."""
        pose_by_name = {pose.name: pose for pose in self.node.poses}
        cameras = []
        for camera in self.node.cameras:
            pose = pose_by_name[camera.name]
            cameras.append(
                {
                    "name": camera.name,
                    "label": camera.label,
                    "topic": camera.topic,
                    "role": pose.teleop_role,
                    "rotation_deg": camera.rotation_deg,
                    "row": camera.row,
                    "column": camera.column,
                }
            )
        poses = [
            {
                "name": pose.name,
                "topic": pose.topic,
                "role": pose.teleop_role,
                "avatar_model": pose.avatar_model,
                "avatar_scale": pose.avatar_scale,
                "avatar_rotation_deg_xyz": list(pose.avatar_rotation_deg_xyz),
                "avatar_offset_xyz": list(pose.avatar_offset_xyz),
            }
            for pose in self.node.poses
        ]
        return cameras, poses
