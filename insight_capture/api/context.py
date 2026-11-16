"""Explicit dependencies shared by dashboard HTTP adapters."""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from insight_capture.runtime.anomaly import ActiveQcMonitor
from insight_capture.runtime.preflight import CapturePreflight
from insight_capture.runtime.take import SessionTakeStore
from insight_capture.postprocess.bags import BagLibrary, PreparedPlaybackManager
from insight_capture.postprocess.optimization import OptimizationManager
from insight_capture.runtime.recording import RecordingManager

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
    take_store: SessionTakeStore
    capture_preflight: CapturePreflight
    active_qc: ActiveQcMonitor
    playback_configuration: Callable[
        [], tuple[list[dict[str, object]], list[dict[str, object]]]
    ]
    _image_capabilities_cache: Optional[Dict[str, object]] = field(default=None, init=False)
    _capture_transition_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)


def capture_transition_lock(context: DashboardContext) -> asyncio.Lock:
    """One API-loop admission lock, also available to lightweight adapters."""
    lock = getattr(context, "_capture_transition_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        context._capture_transition_lock = lock
    return lock
