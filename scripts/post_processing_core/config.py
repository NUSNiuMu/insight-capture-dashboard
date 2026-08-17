"""Post-processing configuration loading."""

import json
import os
from pathlib import Path
import subprocess
import time
from typing import Dict, Optional

DEFAULT_POST_PROCESSING_CONFIG = {
    "rosbag_dir": "rosbags",
    "results_dir": "outputs/results",
    "max_cache_size": 1073741824,
    "recording_storage_id": "mcap",
    "recording_rmw_implementation": "rmw_cyclonedds_cpp",
    "record_topics": [],
    "gesture_recording": {"enabled": False},
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


def probe_recording_root(root: Path) -> Optional[str]:
    """Return an error when the recording staging directory is not durable."""
    staging_root = root / "_staging"
    probe_path = staging_root / (
        f".storage-probe-{os.getpid()}-{time.monotonic_ns()}"
    )
    fd: Optional[int] = None
    try:
        staging_root.mkdir(parents=True, exist_ok=True)
        # A stale bind mount can still satisfy findmnt and stat(root), while
        # directory lookup or writes below it fail with EIO after USB removal.
        with os.scandir(staging_root):
            pass
        fd = os.open(probe_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, b"recording-storage-probe\n")
        os.fsync(fd)
        os.close(fd)
        fd = None
        probe_path.unlink()
    except OSError as exc:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            probe_path.unlink(missing_ok=True)
        except OSError:
            pass
        return f"{type(exc).__name__}: {exc}"
    return None


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
    primary_error = probe_recording_root(primary) if source_matches else None
    fallback = not source_matches or primary_error is not None
    active = primary
    fallback_reason = None
    if not source_matches:
        fallback_reason = (
            f"required source {required_source} does not match "
            f"{mounted_source or 'an unmounted path'}"
        )
    elif primary_error:
        fallback_reason = f"primary storage probe failed: {primary_error}"
    if fallback:
        fallback_value = os.environ.get("INSIGHT_ROSBAG_FALLBACK_DIR", "").strip() or "rosbags"
        active = Path(fallback_value)
        if not active.is_absolute():
            active = project_root / active
        active = active.resolve()
        fallback_error = probe_recording_root(active)
        if fallback_error:
            raise RuntimeError(
                f"Recording storage unavailable ({fallback_reason}); "
                f"fallback probe failed: {fallback_error}"
            )
    return active, {
        "configured_path": str(primary),
        "active_path": str(active),
        "required_source": required_source or None,
        "mounted_source": mounted_source or None,
        "using_fallback": fallback,
        "fallback_reason": fallback_reason,
    }
