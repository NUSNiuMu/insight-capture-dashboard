"""Explicit dependencies shared by dashboard HTTP adapters."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from insight_capture.postprocess.handpose import HandPoseManager
from insight_capture.runtime.anomaly import ActiveQcMonitor, VoiceAlertQueue
from insight_capture.runtime.preflight import CapturePreflight
from insight_capture.runtime.take import SessionTakeStore
from insight_capture.postprocess.bags import PreparedPlaybackManager
from insight_capture.postprocess.optimization import OptimizationManager
from insight_capture.runtime.recording import RecordingManager

from insight_capture.services.dataset_export import UmiExportManager
from insight_capture.services.gripper_extraction import GripperExtractionManager
from insight_capture.services.scoring import ScoringManager


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
    take_store: SessionTakeStore = field(init=False)
    capture_preflight: CapturePreflight = field(init=False)
    voice_alerts: VoiceAlertQueue = field(init=False)
    active_qc: ActiveQcMonitor = field(init=False)

    def __post_init__(self) -> None:
        self.web_root = self.web_root.resolve() if self.web_root else None
        self.project_root = self.project_root.resolve()
        self.results_root = self.results_root.resolve()
        runtime_config = dict(getattr(self.node, "post_processing_config", {}) or {})
        self.take_store = SessionTakeStore(
            self.results_root, runtime_config.get("capture_session")
        )
        self.capture_preflight = CapturePreflight(
            self.node,
            self.recording_manager,
            runtime_config.get("preflight"),
        )
        self.voice_alerts = VoiceAlertQueue()
        self.active_qc = ActiveQcMonitor(
            self.node,
            self.recording_manager,
            self.take_store,
            self.voice_alerts,
            runtime_config.get("active_qc"),
        )
        self.active_qc.start()
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
        self.recording_manager.add_storage_changed_callback(self._set_rosbag_root)

    def _set_rosbag_root(self, rosbag_root: Path) -> None:
        """Keep bag consumers aligned after recording storage failover."""
        root = rosbag_root.resolve()
        self.scoring_manager.rosbag_root = root
        self.prepared_playback_manager.rosbag_root = root
        self.handpose_manager.rosbag_root = root
        self.gripper_extraction_manager.rosbag_root = root
        self.umi_export_manager.rosbag_root = root

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
