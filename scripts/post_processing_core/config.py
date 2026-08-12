"""Post-processing configuration loading."""

import json
from pathlib import Path
from typing import Dict

DEFAULT_POST_PROCESSING_CONFIG = {
    "rosbag_dir": "rosbags",
    "host_rosbag_sync_dir": "",
    "host_rosbag_sync_ssh_target": "",
    "sync_rosbag_to_host": False,
    "results_dir": "outputs/results",
    "max_cache_size": 2147483648,
    "record_topics": [],
    "gesture_recording": {"enabled": False},
    "voice_recording": {"enabled": False},
}


def load_post_processing_config(config_path: Path) -> Dict:
    if not config_path.exists():
        return dict(DEFAULT_POST_PROCESSING_CONFIG)
    with config_path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    merged = dict(DEFAULT_POST_PROCESSING_CONFIG)
    merged.update(payload)
    return merged
