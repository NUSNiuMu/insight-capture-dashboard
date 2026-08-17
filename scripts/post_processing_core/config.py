"""Post-processing configuration loading."""

import json
import os
from pathlib import Path
import subprocess
from typing import Dict

DEFAULT_POST_PROCESSING_CONFIG = {
    "rosbag_dir": "rosbags",
    "results_dir": "outputs/results",
    "max_cache_size": 1073741824,
    "recording_storage_id": "mcap",
    "record_topics": [],
    "gesture_recording": {"enabled": False},
    "voice_recording": {"enabled": False},
    "capture_check": {"enabled": True},
}


def load_post_processing_config(config_path: Path) -> Dict:
    if not config_path.exists():
        return dict(DEFAULT_POST_PROCESSING_CONFIG)
    with config_path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    merged = dict(DEFAULT_POST_PROCESSING_CONFIG)
    merged.update(payload)
    return merged


def resolve_recording_root(primary: Path, project_root: Path):
    """Select the configured drive or an explicit NVMe fallback."""
    primary = primary.resolve()
    required_source = os.environ.get("INSIGHT_ROSBAG_REQUIRED_SOURCE", "").strip()
    mounted_source = ""
    if required_source:
        try:
            mounted_source = subprocess.run(
                ["findmnt", "-no", "SOURCE", "--target", str(primary)],
                check=True,
                capture_output=True,
                text=True,
                timeout=3.0,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            mounted_source = ""

    source_matches = (
        not required_source
        or mounted_source == required_source
        or mounted_source.startswith(f"{required_source}[")
    )
    fallback = not source_matches
    active = primary
    if fallback:
        fallback_value = os.environ.get("INSIGHT_ROSBAG_FALLBACK_DIR", "").strip() or "rosbags"
        active = Path(fallback_value)
        if not active.is_absolute():
            active = project_root / active
        active = active.resolve()
    return active, {
        "configured_path": str(primary),
        "active_path": str(active),
        "required_source": required_source or None,
        "mounted_source": mounted_source or None,
        "using_fallback": fallback,
    }
