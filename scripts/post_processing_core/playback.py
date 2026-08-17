"""Rosbag playback lifecycle and topic remapping."""

import os
import signal
import subprocess
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, TYPE_CHECKING

from .composite_bag import read_metadata, recorded_topics, session_parts

if TYPE_CHECKING:
    from .recording import RecordingManager

def _read_bag_topics(bag_path: Path) -> List[str]:
    return recorded_topics(bag_path)


class PlaybackManager:
    def __init__(self, rosbag_root: Path, ros_domain_id: int,
                 on_stopped: Optional[Callable[[], None]] = None) -> None:
        self.rosbag_root = rosbag_root.resolve()
        self.ros_domain_id = int(ros_domain_id)
        self._on_stopped = on_stopped
        self._lock = threading.Lock()
        self._process: Optional[subprocess.Popen] = None
        self._processes: List[subprocess.Popen] = []
        self._bag_name: str = ""

    def status(self) -> Dict:
        with self._lock:
            self._reap_unlocked()
            return {"state": "playing" if self._process is not None else "idle", "bag_name": self._bag_name}

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
            parts = session_parts(bag_path)
            if not parts:
                raise ValueError(f"Bag has no readable rosbag2 parts: {bag_name}")
            starts = [
                int((read_metadata(part).get("starting_time") or {}).get(
                    "nanoseconds_since_epoch", 0
                ) or 0)
                for part in parts
            ]
            base_start = min((stamp for stamp in starts if stamp), default=0)
            env = os.environ.copy()
            env["ROS_DOMAIN_ID"] = str(self.ros_domain_id)
            processes = []
            for part, start_ns in zip(parts, starts):
                cmd = ["ros2", "bag", "play", str(part)]
                if len(parts) > 1:
                    delay = 1.0 + max(0.0, (start_ns - base_start) / 1e9)
                    cmd += ["--delay", f"{delay:.6f}"]
                if remap_topics:
                    part_topics = set(recorded_topics(part))
                    playback_topics = [
                        topic for topic in remap_topics if topic in part_topics
                    ]
                    if not playback_topics:
                        continue
                    cmd += ["--topics", *playback_topics]
                    cmd += ["--remap"] + [
                        f"{old}:={new}" for old, new in remap_topics.items()
                    ]
                processes.append(subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    env=env,
                ))
            if not processes:
                raise ValueError("Bag contains none of the requested playback topics")
            self._processes = processes
            self._process = processes[0]
            self._bag_name = bag_name
        threading.Thread(
            target=self._monitor, args=(processes,), daemon=True, name="playback_monitor"
        ).start()

    def stop(self) -> None:
        with self._lock:
            processes = list(self._processes)
            if not processes:
                return
            self._process = None
            self._processes = []
            self._bag_name = ""
        for process in processes:
            if process.poll() is not None:
                continue
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
        self._processes = [process for process in self._processes if process.poll() is None]
        if not self._processes:
            self._process = None
            self._bag_name = ""
        else:
            self._process = self._processes[0]

    def _monitor(self, processes: List[subprocess.Popen]) -> None:
        for process in processes:
            process.wait()
        stopped = False
        with self._lock:
            if self._processes == processes:
                self._process = None
                self._processes = []
                self._bag_name = ""
                stopped = True
        if stopped and self._on_stopped:
            try:
                self._on_stopped()
            except Exception:
                pass
