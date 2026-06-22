#!/usr/bin/env python3

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


DEFAULT_ROSBAG_ROOT = "/workspace/rosbags"
DEFAULT_RECORD_TOPICS = [
    "/tf_static",
]


def load_json_config(path: Path) -> Dict[str, object]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def build_default_topics(cameras_config: Dict[str, object]) -> List[str]:
    topics = set(DEFAULT_RECORD_TOPICS)
    for camera in cameras_config.get("cameras", []):
        if not camera.get("enabled", True):
            continue
        namespace = str(camera.get("namespace", "")).strip("/")
        pose_stream = str(camera.get("dashboard_pose_stream", "vio_100hz")).strip("/")
        image_stream = str(camera.get("dashboard_image_stream", "infra1")).strip("/")
        if namespace:
            topics.add(f"/{namespace}/camera/{pose_stream}")
            topics.add(f"/{namespace}/camera/{image_stream}/camera_info")
            if image_stream.startswith("color"):
                topics.add(f"/{namespace}/camera/{image_stream}/image_raw")
                topics.add(f"/{namespace}/camera/{image_stream}/image_rect_raw/compressed")
            else:
                topics.add(f"/{namespace}/camera/{image_stream}/image_rect_raw")
    return sorted(topics)


@dataclass
class RosbagEntry:
    name: str
    path: Path
    size_bytes: int
    modified_time: float
    metadata_path: Optional[Path]

    def to_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "path": str(self.path),
            "size_bytes": self.size_bytes,
            "modified_time": self.modified_time,
            "metadata_path": str(self.metadata_path) if self.metadata_path else None,
        }


class RosbagStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def list_bags(self) -> List[RosbagEntry]:
        entries: List[RosbagEntry] = []
        for child in sorted(self.root.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
            if child.name.startswith("."):
                continue
            if child.is_dir():
                metadata = child / "metadata.yaml"
                db_files = list(child.glob("*.db3")) + list(child.glob("*.mcap"))
                if not metadata.exists() and not db_files:
                    continue
                entries.append(
                    RosbagEntry(
                        name=child.name,
                        path=child,
                        size_bytes=self._path_size(child),
                        modified_time=child.stat().st_mtime,
                        metadata_path=metadata if metadata.exists() else None,
                    )
                )
            elif child.suffix in {".db3", ".mcap"}:
                entries.append(
                    RosbagEntry(
                        name=child.name,
                        path=child,
                        size_bytes=child.stat().st_size,
                        modified_time=child.stat().st_mtime,
                        metadata_path=None,
                    )
                )
        return entries

    def resolve_bag(self, bag_name: str) -> RosbagEntry:
        requested = Path(bag_name)
        if requested.is_absolute():
            candidate = requested.resolve()
        else:
            candidate = (self.root / bag_name).resolve()
        root = self.root.resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("bag path is outside the configured rosbag directory") from exc
        if not candidate.exists():
            raise FileNotFoundError(f"rosbag not found: {bag_name}")
        for entry in self.list_bags():
            if entry.path.resolve() == candidate:
                return entry
        raise ValueError(f"path is not a supported rosbag: {bag_name}")

    @staticmethod
    def _path_size(path: Path) -> int:
        if path.is_file():
            return path.stat().st_size
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


class RecordingManager:
    def __init__(self, store: RosbagStore, topics: List[str], max_cache_size: int = 2147483648) -> None:
        self.store = store
        self.topics = topics
        self.max_cache_size = int(max_cache_size)
        self.process: Optional[subprocess.Popen] = None
        self.output_path: Optional[Path] = None
        self.started_at: Optional[float] = None

    def status(self) -> Dict[str, object]:
        if self.process and self.process.poll() is not None:
            self.process = None
        return {
            "recording": self.process is not None,
            "pid": self.process.pid if self.process else None,
            "output_path": str(self.output_path) if self.output_path else None,
            "started_at": self.started_at,
            "topics": self.topics,
        }

    def start(self, topics: Optional[List[str]] = None) -> Dict[str, object]:
        if self.process and self.process.poll() is None:
            raise RuntimeError("rosbag recording is already running")
        record_topics = topics or self.topics
        if not record_topics:
            raise RuntimeError("no rosbag topics configured")
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        self.output_path = self.store.root / f"insight_record_{timestamp}"
        command = [
            "ros2",
            "bag",
            "record",
            "--output",
            str(self.output_path),
            "--max-cache-size",
            str(self.max_cache_size),
            *record_topics,
        ]
        env = os.environ.copy()
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
            env=env,
        )
        self.started_at = time.time()
        return self.status()

    def stop(self, timeout_sec: float = 8.0) -> Dict[str, object]:
        if not self.process or self.process.poll() is not None:
            self.process = None
            return self.status()
        os.killpg(os.getpgid(self.process.pid), signal.SIGINT)
        try:
            self.process.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            self.process.wait(timeout=3.0)
        self.process = None
        return self.status()


class PostProcessor:
    def __init__(self, store: RosbagStore) -> None:
        self.store = store

    def run(self, action: str, bag_name: str) -> Dict[str, object]:
        bag = self.store.resolve_bag(bag_name)
        readers = {
            "coordinate-alignment": self.coordinate_alignment,
            "trajectory-scoring": self.trajectory_scoring,
            "trajectory-optimization": self.trajectory_optimization,
        }
        if action not in readers:
            raise ValueError(f"unsupported post-processing action: {action}")
        return readers[action](bag)

    def coordinate_alignment(self, bag: RosbagEntry) -> Dict[str, object]:
        return self._base_result("coordinate-alignment", bag, "ready_for_alignment")

    def trajectory_scoring(self, bag: RosbagEntry) -> Dict[str, object]:
        result = self._base_result("trajectory-scoring", bag, "placeholder_score")
        result["score"] = 1.0 if bag.size_bytes > 0 else 0.0
        return result

    def trajectory_optimization(self, bag: RosbagEntry) -> Dict[str, object]:
        return self._base_result("trajectory-optimization", bag, "optimization_pending")

    def _base_result(self, action: str, bag: RosbagEntry, status: str) -> Dict[str, object]:
        files = []
        if bag.path.is_dir():
            files = [str(path.relative_to(bag.path)) for path in sorted(bag.path.rglob("*")) if path.is_file()]
        else:
            files = [bag.path.name]
        return {
            "action": action,
            "status": status,
            "bag": bag.to_dict(),
            "file_count": len(files),
            "sample_files": files[:8],
            "message": "Placeholder result generated from the selected rosbag metadata.",
        }
