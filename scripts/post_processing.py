#!/usr/bin/env python3
"""Compatibility imports for the split post-processing services."""

from _bootstrap import PROJECT_ROOT

from insight_capture.postprocess.bags import (
    PlaybackManager,
    PreparedPlaybackManager,
    list_rosbags,
)
from insight_capture.postprocess.optimization import OptimizationManager
from insight_capture.runtime.recording import (
    DEFAULT_POST_PROCESSING_CONFIG,
    RecordingManager,
    STORAGE_CONFIG_PATH,
    build_default_topics,
    build_recording_topic_catalog,
    discover_live_topics,
    filter_recordable_live_topics,
    load_post_processing_config,
)
from insight_capture.postprocess.bags.catalog import (
    _directory_size_bytes,
    _format_bytes,
    _read_bag_metadata,
    _result_exists,
)
from insight_capture.postprocess.bags.playback import _read_bag_topics
from insight_capture.runtime.recording.manager import _storage_config_args, _trim_startup_skew
from insight_capture.runtime.recording.recorder import (
    _camera_pose_topic,
    _normalize_topic_name,
    _normalize_topics,
    _parse_topic_list_with_types,
    _topic_group,
)

__all__ = [
    "DEFAULT_POST_PROCESSING_CONFIG",
    "OptimizationManager",
    "PlaybackManager",
    "PreparedPlaybackManager",
    "RecordingManager",
    "STORAGE_CONFIG_PATH",
    "build_default_topics",
    "build_recording_topic_catalog",
    "discover_live_topics",
    "filter_recordable_live_topics",
    "list_rosbags",
    "load_post_processing_config",
]
