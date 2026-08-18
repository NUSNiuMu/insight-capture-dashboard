"""Configuration and service factories for the field-capture process."""

from __future__ import annotations

import faulthandler
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, IO, Optional

from insight_capture.core.config import load_setup
from insight_capture.core.paths import PROJECT_ROOT
from insight_capture.runtime.recording import (
    RecordingManager,
    build_default_topics,
    load_post_processing_config,
)
from insight_capture.runtime.recording.storage import resolve_recording_root


@dataclass(frozen=True)
class RuntimeSettings:
    """Resolved paths and configuration used to assemble the live process."""

    config_path: Path
    project_root: Path
    runtime_config_path: Path
    raw_config: Dict[str, object]
    runtime_config: Dict[str, object]
    ros_domain_id: int
    configured_rosbag_root: Path
    rosbag_root: Path
    storage_status: Dict[str, object]
    storage_browse_roots: list[Path]
    results_root: Path
    default_record_topics: list[str]


def configure_crash_diagnostics() -> IO[str]:
    """Persist native crashes and on-demand thread dumps outside Docker logs."""

    crash_log_path = PROJECT_ROOT / "outputs" / "backend_crash.log"
    crash_log_path.parent.mkdir(parents=True, exist_ok=True)
    crash_log = crash_log_path.open("a", buffering=1)
    crash_log.write(f"--- backend start pid={os.getpid()} time={time.time():.0f} ---\n")
    faulthandler.enable(file=crash_log, all_threads=True)
    for crash_signal in (signal.SIGTERM, signal.SIGINT):
        faulthandler.register(
            crash_signal,
            file=crash_log,
            all_threads=True,
            chain=True,
        )
    # SIGUSR1 has no default action after registration, so this only dumps.
    faulthandler.register(
        signal.SIGUSR1,
        file=crash_log,
        all_threads=True,
        chain=False,
    )
    return crash_log


def load_runtime_settings(
    *,
    config_path: Path,
    runtime_config_path: Path,
    rosbag_dir: Optional[str] = None,
) -> RuntimeSettings:
    """Resolve live configuration and writable roots before ROS initializes."""

    config_path = config_path.resolve()
    runtime_config_path = runtime_config_path.resolve()
    project_root = config_path.parents[1]
    raw_config = load_setup(config_path)
    runtime_config = load_post_processing_config(runtime_config_path)
    ros_domain_id = int(raw_config.get("ros_domain_id", 10))

    rosbag_dir_value = (
        rosbag_dir
        or os.environ.get("INSIGHT_ROSBAG_DIR")
        or runtime_config.get("rosbag_dir")
        or "rosbags"
    )
    configured_rosbag_root = Path(str(rosbag_dir_value))
    if not configured_rosbag_root.is_absolute():
        configured_rosbag_root = (project_root / configured_rosbag_root).resolve()
    rosbag_root, storage_status = resolve_recording_root(
        configured_rosbag_root,
        project_root,
    )

    fallback_root = Path(
        os.environ.get("INSIGHT_ROSBAG_FALLBACK_DIR", "").strip() or "rosbags"
    )
    if not fallback_root.is_absolute():
        fallback_root = (project_root / fallback_root).resolve()
    storage_browse_roots = [rosbag_root, fallback_root]
    if not storage_status["using_fallback"]:
        storage_browse_roots.insert(0, configured_rosbag_root)

    results_root = Path(str(runtime_config.get("results_dir", "outputs/results")))
    if not results_root.is_absolute():
        results_root = (project_root / results_root).resolve()
    rosbag_root.mkdir(parents=True, exist_ok=True)
    results_root.mkdir(parents=True, exist_ok=True)

    configured_topics = runtime_config.get("record_topics") or []
    default_topics = (
        list(configured_topics)
        if configured_topics
        else build_default_topics(raw_config)
    )
    return RuntimeSettings(
        config_path=config_path,
        project_root=project_root,
        runtime_config_path=runtime_config_path,
        raw_config=raw_config,
        runtime_config=runtime_config,
        ros_domain_id=ros_domain_id,
        configured_rosbag_root=configured_rosbag_root,
        rosbag_root=rosbag_root,
        storage_status=storage_status,
        storage_browse_roots=storage_browse_roots,
        results_root=results_root,
        default_record_topics=default_topics,
    )


def build_recording_manager(
    settings: RuntimeSettings,
    node: object,
) -> RecordingManager:
    """Build the recorder against the node's explicit image bridge."""

    runtime_config = settings.runtime_config
    return RecordingManager(
        raw_config=settings.raw_config,
        ros_domain_id=settings.ros_domain_id,
        rosbag_root=settings.rosbag_root,
        max_cache_size=int(runtime_config.get("max_cache_size", 2147483648)),
        default_topics=settings.default_record_topics,
        publisher_checker=None,
        image_topics=[camera.topic for camera in node.cameras],
        start_image_recording=node.start_image_recording,
        stop_image_recording=node.stop_image_recording,
        storage_id=str(runtime_config.get("recording_storage_id", "mcap")),
        recording_rmw_implementation=str(
            runtime_config.get(
                "recording_rmw_implementation",
                "rmw_cyclonedds_cpp",
            )
        ),
        storage_status=settings.storage_status,
        storage_resolver=lambda: resolve_recording_root(
            settings.configured_rosbag_root,
            settings.project_root,
        ),
        storage_browse_roots=settings.storage_browse_roots,
    )
