"""Application-service composition for the field-capture process."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, TypedDict

from insight_capture.postprocess.bags import BagLibrary, PreparedPlaybackManager
from insight_capture.postprocess.handpose import HandPoseManager
from insight_capture.postprocess.optimization import OptimizationManager
from insight_capture.runtime.anomaly import ActiveQcMonitor, VoiceAlertQueue
from insight_capture.runtime.preflight import CapturePreflight
from insight_capture.runtime.recording import RecordingManager
from insight_capture.runtime.take import SessionTakeStore
from insight_capture.runtime.tasks import CaptureTaskCatalog
from insight_capture.services.dataset_export import UmiExportManager
from insight_capture.services.gripper_extraction import GripperExtractionManager
from insight_capture.services.scoring import ScoringManager


class DashboardDependencies(TypedDict):
    bag_library: BagLibrary
    scoring_manager: ScoringManager
    prepared_playback_manager: PreparedPlaybackManager
    optimization_manager: OptimizationManager
    handpose_manager: HandPoseManager
    gripper_extraction_manager: GripperExtractionManager
    umi_export_manager: UmiExportManager
    take_store: SessionTakeStore
    capture_preflight: CapturePreflight
    voice_alerts: VoiceAlertQueue
    active_qc: ActiveQcMonitor
    playback_configuration: Callable[
        [], tuple[list[dict[str, object]], list[dict[str, object]]]
    ]


@dataclass
class RuntimeServices:
    """Process-owned services injected into delivery adapters."""

    node: Any
    bag_library: BagLibrary
    scoring_manager: ScoringManager
    prepared_playback_manager: PreparedPlaybackManager
    optimization_manager: OptimizationManager
    handpose_manager: HandPoseManager
    gripper_extraction_manager: GripperExtractionManager
    umi_export_manager: UmiExportManager
    take_store: SessionTakeStore
    capture_preflight: CapturePreflight
    voice_alerts: VoiceAlertQueue
    active_qc: ActiveQcMonitor

    def playback_configuration(
        self,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        """Return stable camera and pose identities used by review bundles."""

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

    def dashboard_dependencies(self) -> DashboardDependencies:
        """Return explicitly named dependencies accepted by DashboardContext."""

        return {
            "bag_library": self.bag_library,
            "scoring_manager": self.scoring_manager,
            "prepared_playback_manager": self.prepared_playback_manager,
            "optimization_manager": self.optimization_manager,
            "handpose_manager": self.handpose_manager,
            "gripper_extraction_manager": self.gripper_extraction_manager,
            "umi_export_manager": self.umi_export_manager,
            "take_store": self.take_store,
            "capture_preflight": self.capture_preflight,
            "voice_alerts": self.voice_alerts,
            "active_qc": self.active_qc,
            "playback_configuration": self.playback_configuration,
        }


def build_runtime_services(
    *,
    node: Any,
    project_root: Path,
    recording_manager: RecordingManager,
    results_root: Path,
    runtime_config: Mapping[str, Any],
) -> RuntimeServices:
    """Construct and start process-owned application services."""

    project_root = project_root.resolve()
    results_root = results_root.resolve()
    task_catalog = CaptureTaskCatalog.load(
        project_root / "config" / "capture_tasks.json",
        managed_root=results_root / "sessions",
    )
    take_store = SessionTakeStore(
        results_root,
        runtime_config.get("capture_session"),
        task_catalog=task_catalog,
    )
    bag_library = BagLibrary(
        recording_manager.storage_browse_roots,
        lambda: recording_manager.rosbag_root,
    )
    voice_alerts = VoiceAlertQueue()
    services = RuntimeServices(
        node=node,
        bag_library=bag_library,
        scoring_manager=ScoringManager(
            rosbag_root=recording_manager.rosbag_root,
            results_root=results_root,
            bag_resolver=bag_library.resolve,
        ),
        prepared_playback_manager=PreparedPlaybackManager(
            rosbag_root=recording_manager.rosbag_root,
            cache_root=results_root / "playback_cache",
            bag_resolver=bag_library.resolve,
            bag_references=lambda: [
                item.bag_id for item in bag_library.locations("all")
            ],
        ),
        optimization_manager=OptimizationManager(
            project_root=project_root,
            pipeline_script=(
                project_root.parent
                / "looper-vio-colmap-handoff"
                / "scripts"
                / "run_pipeline_from_rosbag.py"
            ),
            bag_resolver=bag_library.resolve,
        ),
        handpose_manager=HandPoseManager(
            project_root=project_root,
            rosbag_root=recording_manager.rosbag_root,
            bag_resolver=bag_library.resolve,
        ),
        gripper_extraction_manager=GripperExtractionManager(
            project_root=project_root,
            rosbag_root=recording_manager.rosbag_root,
            bag_resolver=bag_library.resolve,
        ),
        umi_export_manager=UmiExportManager(
            project_root=project_root,
            rosbag_root=recording_manager.rosbag_root,
            bag_resolver=bag_library.resolve,
        ),
        take_store=take_store,
        capture_preflight=CapturePreflight(
            node,
            recording_manager,
            runtime_config.get("preflight"),
        ),
        voice_alerts=voice_alerts,
        active_qc=ActiveQcMonitor(
            node,
            recording_manager,
            take_store,
            voice_alerts,
            runtime_config.get("active_qc"),
        ),
    )
    services.prepared_playback_manager.configure_background(
        recording_manager,
        services.playback_configuration,
    )
    recording_manager.add_recording_completed_callback(
        lambda path: services.prepared_playback_manager.enqueue(
            services.bag_library.reference_for_path(path) or path.name
        )
    )
    services.active_qc.start()
    return services
