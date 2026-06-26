#!/usr/bin/env python3

import json
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, Sequence, Set, Tuple

from camera_setup import camera_base, camera_info_topic, enabled_cameras, image_topic

try:
    import yaml
except Exception:  # pragma: no cover - metadata parsing degrades gracefully
    yaml = None


DEFAULT_POST_PROCESSING_CONFIG = {
    "rosbag_dir": "rosbags",
    "host_rosbag_sync_dir": "",
    "host_rosbag_sync_ssh_target": "",
    "sync_rosbag_to_host": False,
    "results_dir": "outputs/results",
    "max_cache_size": 2147483648,
    "record_topics": [],
}


def load_post_processing_config(config_path: Path) -> Dict:
    if not config_path.exists():
        return dict(DEFAULT_POST_PROCESSING_CONFIG)
    with config_path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    merged = dict(DEFAULT_POST_PROCESSING_CONFIG)
    merged.update(payload)
    return merged


def _format_bytes(size_bytes: int) -> str:
    value = float(max(int(size_bytes), 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0


def _directory_size_bytes(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total


def _result_exists(results_root: Path, category: str, bag_name: str) -> bool:
    candidates = [
        results_root / category / f"{bag_name}.json",
        results_root / category / bag_name,
        results_root / f"{bag_name}_{category}.json",
    ]
    return any(candidate.exists() for candidate in candidates)


def _read_bag_metadata(metadata_path: Path) -> Dict[str, object]:
    if yaml is None or not metadata_path.exists():
        return {}
    try:
        payload = yaml.safe_load(metadata_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    info = payload.get("rosbag2_bagfile_information", {})
    return info if isinstance(info, dict) else {}


def list_rosbags(rosbag_root: Path, results_root: Path) -> List[Dict[str, object]]:
    if not rosbag_root.exists():
        return []
    entries: List[Dict[str, object]] = []
    for bag_dir in sorted(rosbag_root.iterdir(), key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True):
        if not bag_dir.is_dir():
            continue
        metadata_path = bag_dir / "metadata.yaml"
        if not metadata_path.exists():
            continue
        metadata = _read_bag_metadata(metadata_path)
        duration_ns = int((metadata.get("duration") or {}).get("nanoseconds", 0) or 0)
        message_count = int(metadata.get("message_count", 0) or 0)
        topics = metadata.get("topics_with_message_count") or []
        size_bytes = _directory_size_bytes(bag_dir)
        labeled = (
            _result_exists(results_root, "labels", bag_dir.name)
            or _result_exists(results_root, "label", bag_dir.name)
            or _result_exists(results_root, "labeled", bag_dir.name)
        )
        scored = _result_exists(results_root, "scores", bag_dir.name) or _result_exists(results_root, "scoring", bag_dir.name)
        optimized = _result_exists(results_root, "optimized", bag_dir.name) or _result_exists(results_root, "optimization", bag_dir.name)
        entries.append(
            {
                "name": bag_dir.name,
                "path": str(bag_dir),
                "size_bytes": size_bytes,
                "size_label": _format_bytes(size_bytes),
                "duration_s": duration_ns / 1_000_000_000.0,
                "message_count": message_count,
                "topic_count": len(topics) if isinstance(topics, list) else 0,
                "modified_at_epoch_s": bag_dir.stat().st_mtime,
                "labeled": labeled,
                "scored": scored,
                "optimized": optimized,
                "label": (
                    f"{'labeled' if labeled else 'unlabeled'} / "
                    f"{'scored' if scored else 'unscored'} / "
                    f"{'optimized' if optimized else 'not optimized'}"
                ),
            }
        )
    return entries


def _normalize_topic_name(topic: str) -> str:
    value = str(topic or "").strip()
    if not value:
        return ""
    if not value.startswith("/"):
        return f"/{value}"
    return value


def _normalize_topics(topics: Sequence[str]) -> List[str]:
    ordered: List[str] = []
    seen: Set[str] = set()
    for topic in topics:
        normalized = _normalize_topic_name(topic)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _camera_pose_topic(namespace: str, pose_stream: str) -> str:
    value = str(pose_stream or "").strip()
    if not value:
        return ""
    if value.startswith("/"):
        return value
    return f"{camera_base(namespace)}/{value}"


def build_default_topics(raw_config: Dict) -> List[str]:
    topics: List[str] = ["/tf_static"]
    for camera in enabled_cameras(raw_config):
        namespace = str(camera["namespace"])
        pose_topic = _camera_pose_topic(namespace, str(camera.get("dashboard_pose_stream", "vio_100hz")))
        if pose_topic:
            topics.append(pose_topic)

        image_stream = str(camera.get("dashboard_image_stream", "color_compressed"))
        topics.append(f"{camera_base(namespace)}/imu")
        topics.append(camera_info_topic(namespace, image_stream))
        topics.append(image_topic(namespace, image_stream))
    return _normalize_topics(topics)


def filter_recordable_live_topics(raw_config: Dict, live_topics: Sequence[str]) -> List[str]:
    enabled = {
        str(camera["namespace"]): camera
        for camera in enabled_cameras(raw_config)
    }
    filtered: List[str] = []
    for topic in live_topics:
        normalized = _normalize_topic_name(topic)
        if normalized == "/tf_static":
            filtered.append(normalized)
            continue
        for namespace in enabled:
            prefix = f"/{namespace}/camera/"
            if normalized.startswith(prefix):
                filtered.append(normalized)
                break
    return _normalize_topics(filtered)


def build_recording_topic_catalog(
    raw_config: Dict,
    topics: Sequence[str],
    default_selected_topics: Optional[Sequence[str]] = None,
) -> Dict[str, object]:
    normalized_topics = _normalize_topics(topics)
    default_selected = set(_normalize_topics(default_selected_topics or []))
    topics_by_camera: Dict[str, List[Dict[str, str]]] = {}
    enabled = enabled_cameras(raw_config)
    cameras: List[Dict[str, object]] = []
    other: List[Dict[str, str]] = []

    for camera in enabled:
        namespace = str(camera["namespace"])
        topics_by_camera[namespace] = []

    for topic in normalized_topics:
        if topic == "/tf_static":
            other.append(
                {
                    "name": topic,
                    "label": "tf_static",
                    "camera": "",
                    "tail": "tf_static",
                    "short_name": "Other - tf_static",
                    "group": "Other",
                    "default_selected": topic in default_selected,
                }
            )
            continue

        matched = False
        for camera in enabled:
            namespace = str(camera["namespace"])
            prefix = f"/{namespace}/camera/"
            if not topic.startswith(prefix):
                continue
            tail = topic[len(prefix) :]
            topics_by_camera[namespace].append(
                {
                    "name": topic,
                    "label": tail,
                    "camera": namespace,
                    "tail": tail,
                    "short_name": f"{namespace} - {tail}",
                    "group": namespace,
                    "default_selected": topic in default_selected,
                }
            )
            matched = True
            break
        if not matched:
            tail = topic.lstrip("/")
            other.append(
                {
                    "name": topic,
                    "label": tail,
                    "camera": "",
                    "tail": tail,
                    "short_name": f"Other - {tail}",
                    "group": "Other",
                    "default_selected": topic in default_selected,
                }
            )

    for camera in enabled:
        namespace = str(camera["namespace"])
        entries = sorted(topics_by_camera[namespace], key=lambda item: item["tail"])
        cameras.append(
            {
                "name": camera["name"],
                "namespace": namespace,
                "label": camera.get("dashboard_label", camera.get("label", camera["name"])),
                "detected": bool(entries),
                "topics": entries,
            }
        )

    other = sorted(other, key=lambda item: item["tail"])
    return {
        "cameras": cameras,
        "other": other,
        "topics": normalized_topics,
        "default_selected_topics": [topic for topic in normalized_topics if topic in default_selected],
    }


def _parse_topic_list_with_types(output: str) -> List[str]:
    topics: List[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if " [" in line:
            name = line.split(" [", 1)[0].strip()
        else:
            name = line
        if name:
            topics.append(name)
    return topics


def ros2_topic_has_publishers(topic: str, ros_domain_id: int, timeout_sec: float = 0.4) -> bool:
    env = os.environ.copy()
    env["ROS_DOMAIN_ID"] = str(int(ros_domain_id))
    try:
        result = subprocess.run(
            ["ros2", "topic", "info", topic, "--verbose"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_sec,
            env=env,
            check=False,
        )
    except Exception:
        return False
    text = result.stdout or ""
    for line in text.splitlines():
        lowered = line.strip().lower()
        if not lowered.startswith("publisher count:"):
            continue
        _, _, count_text = lowered.partition(":")
        try:
            return int(count_text.strip()) > 0
        except ValueError:
            return False
    return False


def discover_live_topics(
    raw_config: Dict,
    ros_domain_id: int,
    publisher_checker: Optional[Callable[[str], bool]] = None,
) -> Dict[str, object]:
    env = os.environ.copy()
    env["ROS_DOMAIN_ID"] = str(int(ros_domain_id))
    try:
        result = subprocess.run(
            ["ros2", "topic", "list", "--no-daemon", "-t"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=2.0,
            env=env,
            check=False,
        )
    except Exception:
        return build_recording_topic_catalog(raw_config, [], build_default_topics(raw_config))

    discovered = _parse_topic_list_with_types(result.stdout or "")
    filtered = filter_recordable_live_topics(raw_config, discovered)
    if publisher_checker is not None:
        with_publishers = []
        for topic in filtered:
            if topic == "/tf_static":
                with_publishers.append(topic)
                continue
            try:
                if publisher_checker(topic):
                    with_publishers.append(topic)
            except Exception:
                continue
        filtered = with_publishers
    return build_recording_topic_catalog(raw_config, filtered, build_default_topics(raw_config))


class RecordingManager:
    def __init__(
        self,
        raw_config: Dict,
        ros_domain_id: int,
        rosbag_root: Path,
        max_cache_size: int,
        default_topics: Sequence[str],
        host_sync_dir: Optional[Path] = None,
        host_sync_ssh_target: str = "",
        sync_to_host_on_stop: bool = False,
        publisher_checker: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self.raw_config = raw_config
        self.ros_domain_id = int(ros_domain_id)
        self.rosbag_root = rosbag_root.resolve()
        self.rosbag_root.mkdir(parents=True, exist_ok=True)
        self.max_cache_size = int(max_cache_size)
        self.default_topics = _normalize_topics(default_topics)
        self.host_sync_dir = host_sync_dir.resolve() if host_sync_dir else None
        self.host_sync_ssh_target = str(host_sync_ssh_target or "").strip()
        self.sync_to_host_on_stop = bool(sync_to_host_on_stop)
        self.publisher_checker = publisher_checker
        self.process: Optional[subprocess.Popen] = None
        self.output_path: Optional[str] = None
        self.started_at: Optional[float] = None
        self.current_topics: List[str] = []
        self.topic_catalog = build_recording_topic_catalog(raw_config, [], self.default_topics)
        self._output_lines: Deque[str] = deque(maxlen=120)
        self._stdout_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._last_topic_refresh_monotonic: float = 0.0
        self.last_sync_status: Dict[str, object] = {
            "state": "idle",
            "message": "Host sync idle",
            "source_path": None,
            "target_path": None,
            "finished_at": None,
        }

    def _cleanup_if_exited_unlocked(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            return
        self.process = None

    def _drain_stdout(self, process: subprocess.Popen) -> None:
        stream = process.stdout
        if stream is None:
            return
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                self._output_lines.append(line.rstrip())
        finally:
            with contextlib_suppress():
                stream.close()

    def refresh_topic_catalog(self, force: bool = False) -> Dict[str, object]:
        now = time.monotonic()
        with self._lock:
            if (
                not force
                and self.topic_catalog.get("topics")
                and (now - self._last_topic_refresh_monotonic) < 1.0
            ):
                return self.topic_catalog
        catalog = discover_live_topics(
            self.raw_config,
            self.ros_domain_id,
            publisher_checker=self.publisher_checker,
        )
        with self._lock:
            self.topic_catalog = catalog
            self._last_topic_refresh_monotonic = now
        return catalog

    def current_topic_catalog(self, refresh: bool = True) -> Dict[str, object]:
        if refresh:
            return self.refresh_topic_catalog()
        with self._lock:
            return self.topic_catalog

    def start(self, topics: Optional[Sequence[str]] = None) -> Dict[str, object]:
        selected_topics = self.default_topics if topics is None else _normalize_topics(topics)
        if not selected_topics:
            raise ValueError("No topics selected for recording.")
        with self._lock:
            self._cleanup_if_exited_unlocked()
            if self.process is not None and self.process.poll() is None:
                raise RuntimeError("Recording is already running.")

            timestamp = time.strftime("%Y%m%d_%H%M%S")
            output_path = self.rosbag_root / f"insight_record_{timestamp}"
            env = os.environ.copy()
            env["ROS_DOMAIN_ID"] = str(self.ros_domain_id)
            cmd = [
                "ros2",
                "bag",
                "record",
                "--output",
                str(output_path),
                "--max-cache-size",
                str(self.max_cache_size),
                *selected_topics,
            ]
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
                env=env,
            )
            self.process = process
            self.output_path = str(output_path)
            self.started_at = time.time()
            self.current_topics = list(selected_topics)
            self._output_lines.clear()
            self.last_sync_status = {
                "state": "idle",
                "message": "Host sync idle",
                "source_path": str(output_path),
                "target_path": None,
                "finished_at": None,
            }
            self._stdout_thread = threading.Thread(
                target=self._drain_stdout,
                args=(process,),
                daemon=True,
                name="rosbag_record_stdout",
            )
            self._stdout_thread.start()
        return self.status()

    def stop(self, timeout_sec: float = 8.0) -> Dict[str, object]:
        process: Optional[subprocess.Popen] = None
        with self._lock:
            self._cleanup_if_exited_unlocked()
            process = self.process
            if process is None or process.poll() is not None:
                self.process = None
                return self.status()

        try:
            os.killpg(os.getpgid(process.pid), signal.SIGINT)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=max(float(timeout_sec), 0.1))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            with contextlib_suppress(subprocess.TimeoutExpired):
                process.wait(timeout=3.0)

        with self._lock:
            self.process = None
            output_path = self.output_path
        if output_path and self.sync_to_host_on_stop:
            self.sync_recording_to_host(output_path)
        return self.status()

    def _build_sync_target_path(self, source_path: Path) -> Path:
        assert self.host_sync_dir is not None
        target_path = self.host_sync_dir / source_path.name
        if not target_path.exists():
            return target_path
        suffix = time.strftime("%Y%m%d_%H%M%S")
        return self.host_sync_dir / f"{source_path.name}_sync_{suffix}"

    def _remote_sync_target(self, source_path: Path) -> str:
        return f"{self.host_sync_ssh_target.rstrip('/')}/{source_path.name}"

    def _sync_recording_to_remote_host(self, source_path: Path) -> Dict[str, object]:
        target_path = self._remote_sync_target(source_path)
        parent_target = self.host_sync_ssh_target.rstrip("/")
        mkdir_cmd = [
            "ssh",
            self.host_sync_ssh_target.split(":", 1)[0],
            "mkdir",
            "-p",
            parent_target.split(":", 1)[1] if ":" in parent_target else parent_target,
        ]
        rsync_cmd = [
            "rsync",
            "-a",
            "--info=stats1",
            f"{source_path}/",
            target_path,
        ]
        try:
            subprocess.run(
                mkdir_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=15.0,
                check=True,
            )
            result = subprocess.run(
                rsync_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=300.0,
                check=True,
            )
            summary = (result.stdout or "").strip().splitlines()
            summary_text = summary[-1] if summary else "rsync complete"
            return {
                "state": "ok",
                "message": f"Synced rosbag to host via ssh: {target_path} ({summary_text})",
                "source_path": str(source_path),
                "target_path": target_path,
                "finished_at": time.time(),
            }
        except Exception as exc:
            return {
                "state": "error",
                "message": f"SSH host sync failed: {exc}",
                "source_path": str(source_path),
                "target_path": target_path,
                "finished_at": time.time(),
            }

    def sync_recording_to_host(self, output_path: Optional[str] = None) -> Dict[str, object]:
        source_raw = output_path
        with self._lock:
            if source_raw is None:
                source_raw = self.output_path
            if not source_raw:
                raise RuntimeError("No recorded rosbag is available to sync.")
            source_path = Path(source_raw).resolve()
            self.last_sync_status = {
                "state": "syncing",
                "message": "Syncing rosbag to host...",
                "source_path": str(source_path),
                "target_path": None,
                "finished_at": None,
            }

        if self.host_sync_dir is None and not self.host_sync_ssh_target:
            status = {
                "state": "disabled",
                "message": "Host sync directory is not configured.",
                "source_path": str(source_path),
                "target_path": None,
                "finished_at": time.time(),
            }
            with self._lock:
                self.last_sync_status = status
            return status
        if not source_path.exists():
            status = {
                "state": "error",
                "message": f"Recorded rosbag path does not exist: {source_path}",
                "source_path": str(source_path),
                "target_path": None,
                "finished_at": time.time(),
            }
            with self._lock:
                self.last_sync_status = status
            return status

        if self.host_sync_ssh_target:
            status = self._sync_recording_to_remote_host(source_path)
        else:
            try:
                assert self.host_sync_dir is not None
                self.host_sync_dir.mkdir(parents=True, exist_ok=True)
                target_path = self._build_sync_target_path(source_path)
                shutil.copytree(source_path, target_path)
                status = {
                    "state": "ok",
                    "message": f"Synced rosbag to host: {target_path}",
                    "source_path": str(source_path),
                    "target_path": str(target_path),
                    "finished_at": time.time(),
                }
            except Exception as exc:
                status = {
                    "state": "error",
                    "message": f"Host sync failed: {exc}",
                    "source_path": str(source_path),
                    "target_path": None,
                    "finished_at": time.time(),
                }

        with self._lock:
            self.last_sync_status = status
        return status

    def status(self) -> Dict[str, object]:
        with self._lock:
            self._cleanup_if_exited_unlocked()
            process = self.process
            catalog = self.topic_catalog
            output_lines = list(self._output_lines)
            recording = bool(process is not None and process.poll() is None)
            pid = process.pid if recording else None
            return {
                "recording": recording,
                "pid": pid,
                "output_path": self.output_path,
                "started_at": self.started_at,
                "topics": list(self.current_topics),
                "topic_catalog": catalog,
                "recent_output": output_lines,
                "host_sync_dir": None if self.host_sync_dir is None else str(self.host_sync_dir),
                "host_sync_ssh_target": self.host_sync_ssh_target or None,
                "sync_to_host_on_stop": self.sync_to_host_on_stop,
                "sync_status": dict(self.last_sync_status),
            }


class contextlib_suppress:
    def __init__(self, *exceptions):
        self.exceptions = exceptions or (Exception,)

    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, _tb):
        return exc_type is not None and issubclass(exc_type, self.exceptions)


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

    def start(self, bag_name: str, recording_manager: "RecordingManager") -> None:
        with self._lock:
            with recording_manager._lock:
                recording_manager._cleanup_if_exited_unlocked()
                if recording_manager.process is not None and recording_manager.process.poll() is None:
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


class OptimizationManager:
    """Runs the looper-vio-colmap pipeline as a background subprocess."""

    STEP_NAMES = [
        "Extracting VIO",
        "Extracting color images",
        "Running COLMAP",
        "Aligning trajectories (Sim3)",
    ]
    _STEP_MARKERS = [
        "1/3 提取 VIO",
        "2/3 提取 color 图片",
        "3/3 运行 COLMAP CLI",
        "4/5 Sim3 对齐 COLMAP 轨迹",
    ]
    _MAX_LOG = 60

    def __init__(
        self,
        project_root: Path,
        pipeline_script: Path,
        on_finished: Optional[Callable[[bool], None]] = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.pipeline_script = pipeline_script.resolve()
        self._on_finished = on_finished
        self._lock = threading.Lock()
        self._process: Optional[subprocess.Popen] = None
        self._state: str = "idle"
        self._step: int = 0
        self._run_name: str = ""
        self._log: List[str] = []
        self._result: Dict = {}

    def status(self) -> Dict:
        with self._lock:
            return {
                "state": self._state,
                "step": self._step,
                "step_name": self.STEP_NAMES[self._step - 1] if 1 <= self._step <= 4 else "",
                "total_steps": 4,
                "run_name": self._run_name,
                "log_tail": list(self._log[-30:]),
                "result": dict(self._result),
            }

    def start(
        self,
        bag_name: str,
        run_name: str,
        vio_topic: str,
        image_topic_str: str,
        output_hz: float = 5.0,
    ) -> None:
        with self._lock:
            if self._state == "running":
                raise RuntimeError("Optimization already running")
            bag_path = self.project_root / "rosbags" / bag_name
            if not bag_path.exists():
                raise ValueError(f"Bag not found: {bag_name}")
            hz_label = str(int(output_hz)) if output_hz == int(output_hz) else str(output_hz).replace(".", "p")
            self._result = {
                "trajectory_3d": f"/optimization-runs/{run_name}/viz/color_{hz_label}hz_vs_vio100/trajectory_3d.png",
                "trajectory_2d": f"/optimization-runs/{run_name}/viz/color_{hz_label}hz_vs_vio100/trajectory_2d.png",
                "colmap_log": f"/optimization-runs/{run_name}/colmap/color_{hz_label}hz/colmap.log",
            }
            cmd = [
                sys.executable,
                str(self.pipeline_script),
                "--bag", str(bag_path),
                "--name", run_name,
                "--vio-topic", vio_topic,
                "--image-topic", image_topic_str,
                "--colmap-runner", "local",
                "--output-hz", str(output_hz),
                "--make-plots", "false",
                "--overwrite", "true",
            ]
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=str(self.project_root),
                env=os.environ.copy(),
                start_new_session=True,
            )
            self._process = process
            self._state = "running"
            self._step = 0
            self._run_name = run_name
            self._log = []
        threading.Thread(
            target=self._monitor, args=(process,), daemon=True, name="optimization_monitor"
        ).start()

    def stop(self) -> None:
        with self._lock:
            process = self._process
        if process is None:
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGINT)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        with self._lock:
            if self._process is process:
                self._process = None
                self._state = "idle"
                self._step = 0

    def _monitor(self, process: subprocess.Popen) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip()
            with self._lock:
                self._log.append(line)
                if len(self._log) > self._MAX_LOG:
                    self._log = self._log[-self._MAX_LOG:]
                for i, marker in enumerate(self._STEP_MARKERS):
                    if marker in line:
                        self._step = i + 1
                        break
        return_code = process.wait()
        success = return_code == 0
        with self._lock:
            if self._process is process:
                self._process = None
                self._state = "done" if success else "error"
                if success:
                    self._step = 4
        if self._on_finished:
            try:
                self._on_finished(success)
            except Exception:
                pass
