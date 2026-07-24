"""Rosbag playback lifecycle and topic remapping."""

import os
import signal
import subprocess
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, TYPE_CHECKING

try:
    import yaml
except Exception:  # pragma: no cover - metadata parsing degrades gracefully
    yaml = None

if TYPE_CHECKING:
    from .recording import RecordingManager

def _read_bag_topics(bag_path: Path) -> List[str]:
    metadata_path = bag_path / "metadata.yaml"
    if not metadata_path.exists():
        return []
    try:
        with open(metadata_path, "r") as f:
            meta = yaml.safe_load(f) if yaml else {}
        topics = []
        for item in (meta.get("rosbag2_bagfile_information", {})
                     .get("topics_with_message_count", [])):
            name = item.get("topic_metadata", {}).get("name", "")
            if name:
                topics.append(name)
        return topics
    except Exception:
        return []


class PlaybackManager:
    def __init__(self, rosbag_root: Path, ros_domain_id: int,
                 on_stopped: Optional[Callable[[], None]] = None) -> None:
        self.rosbag_root = rosbag_root.resolve()
        self.ros_domain_id = int(ros_domain_id)
        self._on_stopped = on_stopped
        self._lock = threading.Lock()
        self._process: Optional[subprocess.Popen] = None
        self._bag_name: str = ""

    def status(self) -> Dict:
        with self._lock:
            self._reap_unlocked()
            return {"state": "playing" if self._process is not None else "idle", "bag_name": self._bag_name}

    def get_bag_time_range(self, bag_name: str) -> Optional[Tuple[int, int]]:
        bag_path = (self.rosbag_root / bag_name).resolve()
        topics = _read_bag_topics(bag_path)  # reuse metadata reader
        meta_path = bag_path / "metadata.yaml"
        if not meta_path.exists():
            return None
        try:
            with open(meta_path, "r") as f:
                meta = yaml.safe_load(f) if yaml else {}
            info = meta.get("rosbag2_bagfile_information", {})
            start_ns = info.get("starting_time", {}).get("nanoseconds_since_epoch", 0)
            duration_ns = info.get("duration", {}).get("nanoseconds", 0)
            margin_ns = int(2e9)  # 2-second margin each side
            return (start_ns - margin_ns, start_ns + duration_ns + margin_ns)
        except Exception:
            return None

    def start(
        self,
        bag_name: str,
        recording_manager: "RecordingManager",
        remap_topics: Optional[Dict[str, str]] = None,
    ) -> None:
        with self._lock:
            with recording_manager._lock:
                recording_manager._cleanup_if_exited_unlocked()
                if recording_manager.processes:
                    raise RuntimeError("Cannot start playback while recording is active.")
            self._reap_unlocked()
            if self._process is not None:
                raise RuntimeError("Playback already running.")
            bag_path = (self.rosbag_root / bag_name).resolve()
            if not bag_path.exists():
                raise ValueError(f"Bag not found: {bag_name}")
            env = os.environ.copy()
            env["ROS_DOMAIN_ID"] = str(self.ros_domain_id)
            cmd = ["ros2", "bag", "play", str(bag_path)]
            if remap_topics:
                # Remap playback topics so live publishers cannot collide.
                cmd += ["--remap"] + [f"{old}:={new}" for old, new in remap_topics.items()]
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
            self._process = process
            self._bag_name = bag_name
        threading.Thread(target=self._monitor, args=(process,), daemon=True, name="playback_monitor").start()

    def stop(self) -> None:
        with self._lock:
            process = self._process
            if process is None:
                return
            self._process = None
            self._bag_name = ""
        if process.poll() is None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def _reap_unlocked(self) -> None:
        if self._process is not None and self._process.poll() is not None:
            self._process = None
            self._bag_name = ""

    def _monitor(self, process: subprocess.Popen) -> None:
        process.wait()
        stopped = False
        with self._lock:
            if self._process is process:
                self._process = None
                self._bag_name = ""
                stopped = True
        if stopped and self._on_stopped:
            try:
                self._on_stopped()
            except Exception:
                pass
