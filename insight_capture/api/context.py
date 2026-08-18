"""Explicit dependencies shared by dashboard HTTP adapters."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from insight_capture.postprocess.handpose import HandPoseManager
from insight_capture.runtime.anomaly import ActiveQcMonitor, VoiceAlertQueue
from insight_capture.runtime.preflight import CapturePreflight
from insight_capture.runtime.take import SessionTakeStore
from insight_capture.postprocess.bags import BagLibrary, PreparedPlaybackManager
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
    bag_library: BagLibrary
    results_root: Path
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
    _image_capabilities_cache: Optional[Dict[str, object]] = field(default=None, init=False)
