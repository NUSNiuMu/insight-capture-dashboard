#!/usr/bin/env python3

import json
import os
import re
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
from perf_tracker import track

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
    stack = [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            total += entry.stat().st_size
                        elif entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                    except OSError:
                        continue
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
        # Unlike labeled/scored, mere file existence isn't enough here: the
        # persisted report (written by /api/integrity/run) says pass or
        # fail, and the badge must show which. None = never checked.
        integrity: Optional[bool] = None
        integrity_path = results_root / "integrity" / f"{bag_dir.name}.json"
        if integrity_path.exists():
            try:
                integrity = bool(json.loads(integrity_path.read_text()).get("ok"))
            except (OSError, ValueError, AttributeError):
                integrity = None
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
                "integrity": integrity,
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
        cov_stream = str(camera.get("dashboard_cov_stream", "vio_image_cov"))
        topics.append(f"{camera_base(namespace)}/{cov_stream}")
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


def discover_live_topics(
    raw_config: Dict,
    ros_domain_id: int,
    publisher_checker: Optional[Callable[[str], bool]] = None,
) -> Dict[str, object]:
    env = os.environ.copy()
    env["ROS_DOMAIN_ID"] = str(int(ros_domain_id))
    try:
        result = subprocess.run(
            ["ros2", "topic", "list", "-t"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5.0,
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


def _topic_group(topic: str) -> str:
    """Group a topic by its ROS namespace (first path segment) -- this is
    how recording is split into one `ros2 bag record` process per camera:
    /insight3_a/camera/imu -> "insight3_a"; /tf_static -> "tf_static".

    A single `ros2 bag record` handling all cameras' topics at once was
    measured to drop the large image messages under load (each camera's
    high-rate IMU crowding out the recorder's ability to service the bigger,
    less frequent image callbacks in time) even though CPU and disk
    throughput both had headroom -- three independent processes each only
    have to keep up with one camera's own topics, which is well within the
    proven-fine workload of two cameras sharing a single recorder.
    """
    return topic.strip("/").split("/", 1)[0]


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
        image_topics: Optional[Sequence[str]] = None,
        start_image_recording: Optional[Callable[[Dict[str, str]], None]] = None,
        stop_image_recording: Optional[Callable[[], Dict[str, object]]] = None,
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
        # Image topics are routed to the dashboard's own already-open
        # subscription instead of a second `ros2 bag record` reader -- see
        # inprocess_bag_writer.py for why a second reader on these topics
        # causes drops. Falls back to the normal subprocess path (grouped in
        # with everything else) if no node is wired in, e.g. under test.
        self._image_topics: Set[str] = set(_normalize_topics(image_topics or []))
        self._start_image_recording = start_image_recording
        self._stop_image_recording = stop_image_recording
        self._image_writer_active = False
        # One `ros2 bag record` per camera (see _topic_group) instead of one
        # process for every selected topic -- keyed by group name so start/
        # stop/status can address them individually.
        self.processes: Dict[str, subprocess.Popen] = {}
        self._staging_dir: Optional[Path] = None
        self.output_path: Optional[str] = None
        self.started_at: Optional[float] = None
        self.current_topics: List[str] = []
        self.topic_catalog = build_recording_topic_catalog(raw_config, [], self.default_topics)
        self._output_lines: Deque[str] = deque(maxlen=200)
        self._stdout_threads: List[threading.Thread] = []
        self._lock = threading.Lock()
        self._merge_thread: Optional[threading.Thread] = None
        self.merge_state: str = "idle"  # idle | merging | done | error
        self.merge_error: Optional[str] = None
        self._last_topic_refresh_monotonic: float = 0.0
        self.last_sync_status: Dict[str, object] = {
            "state": "idle",
            "message": "Host sync idle",
            "source_path": None,
            "target_path": None,
            "finished_at": None,
        }

    def _cleanup_if_exited_unlocked(self) -> None:
        self.processes = {
            group: process for group, process in self.processes.items() if process.poll() is None
        }

    def _drain_stdout(self, label: str, process: subprocess.Popen) -> None:
        stream = process.stdout
        if stream is None:
            return
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                self._output_lines.append(f"[{label}] {line.rstrip()}")
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

    def start(
        self,
        topics: Optional[Sequence[str]] = None,
        bag_name: Optional[str] = None,
    ) -> Dict[str, object]:
        selected_topics = self.default_topics if topics is None else _normalize_topics(topics)
        if not selected_topics:
            raise ValueError("No topics selected for recording.")
        with self._lock:
            self._cleanup_if_exited_unlocked()
            if self.processes or self._image_writer_active:
                raise RuntimeError("Recording is already running.")
            if self.merge_state == "merging":
                raise RuntimeError("Still merging the previous recording -- wait for it to finish.")

            timestamp = time.strftime("%Y%m%d_%H%M%S")
            if bag_name:
                safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in bag_name.strip())
                name = safe or f"insight_record_{timestamp}"
            else:
                name = f"insight_record_{timestamp}"
            output_path = self.rosbag_root / name
            staging_dir = self.rosbag_root / "_staging" / name
            staging_dir.mkdir(parents=True, exist_ok=True)

            if self._start_image_recording is not None:
                image_topics = [t for t in selected_topics if t in self._image_topics]
                other_topics = [t for t in selected_topics if t not in self._image_topics]
            else:
                image_topics = []
                other_topics = list(selected_topics)

            groups: Dict[str, List[str]] = {}
            for topic in other_topics:
                groups.setdefault(_topic_group(topic), []).append(topic)

            image_writer_active = False
            if image_topics:
                # One writer/thread per camera group (mirrors the per-camera
                # `ros2 bag record` split above) -- a single shared writer
                # was measured to bottleneck the large uncompressed streams
                # behind each other (~13-16Hz instead of ~20Hz native).
                topic_output_paths = {
                    topic: str(staging_dir / f"_images_{_topic_group(topic)}") for topic in image_topics
                }
                self._start_image_recording(topic_output_paths)
                image_writer_active = True

            env = os.environ.copy()
            env["ROS_DOMAIN_ID"] = str(self.ros_domain_id)
            processes: Dict[str, subprocess.Popen] = {}
            stdout_threads: List[threading.Thread] = []
            for group, group_topics in groups.items():
                cmd = [
                    "ros2",
                    "bag",
                    "record",
                    "--output",
                    str(staging_dir / group),
                    "--max-cache-size",
                    str(self.max_cache_size),
                    *group_topics,
                ]
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                    env=env,
                )
                processes[group] = process
                thread = threading.Thread(
                    target=self._drain_stdout,
                    args=(group, process),
                    daemon=True,
                    name=f"rosbag_record_stdout_{group}",
                )
                thread.start()
                stdout_threads.append(thread)

            self.processes = processes
            self._stdout_threads = stdout_threads
            self._staging_dir = staging_dir
            self._image_writer_active = image_writer_active
            self.output_path = str(output_path)
            self.started_at = time.time()
            self.current_topics = list(selected_topics)
            self._output_lines.clear()
            self.merge_state = "idle"
            self.merge_error = None
            self.last_sync_status = {
                "state": "idle",
                "message": "Host sync idle",
                "source_path": str(output_path),
                "target_path": None,
                "finished_at": None,
            }
        return self.status()

    def stop(self, timeout_sec: float = 8.0) -> Dict[str, object]:
        with self._lock:
            self._cleanup_if_exited_unlocked()
            processes = dict(self.processes)
            staging_dir = self._staging_dir
            output_path = self.output_path
            image_writer_active = self._image_writer_active
            self._image_writer_active = False
            if not processes and not image_writer_active:
                return self.status()

        if image_writer_active and self._stop_image_recording is not None:
            try:
                result = self._stop_image_recording()
                dropped = int(result.get("dropped", 0)) if result else 0
                if dropped:
                    self._output_lines.append(f"[_images] WARNING: dropped {dropped} message(s) (writer queue full)")
            except Exception as exc:  # noqa: BLE001 - surfaced via output log, not fatal to stop()
                self._output_lines.append(f"[_images] ERROR stopping writer: {exc}")

        # Signal every per-camera recorder up front (not one-at-a-time) so
        # their shutdown/flush windows overlap instead of serializing the
        # wait across N processes.
        for process in processes.values():
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGINT)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + max(float(timeout_sec), 0.1)
        still_running = []
        for process in processes.values():
            remaining = max(deadline - time.monotonic(), 0.0)
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                still_running.append(process)
        if still_running:
            for process in still_running:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
            for process in still_running:
                with contextlib_suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=3.0)

        with self._lock:
            self.processes = {}
            self.merge_state = "merging"
            self.merge_error = None

        self._merge_thread = threading.Thread(
            target=self._merge_worker,
            args=(staging_dir, Path(output_path) if output_path else None),
            daemon=True,
            name="rosbag_merge",
        )
        self._merge_thread.start()
        return self.status()

    def _merge_worker(self, staging_dir: Optional[Path], output_path: Optional[Path]) -> None:
        try:
            if staging_dir is None or output_path is None:
                raise RuntimeError("Missing staging directory or output path.")
            part_bags = sorted(
                p for p in staging_dir.iterdir() if p.is_dir() and (p / "metadata.yaml").is_file()
            )
            missing = sorted(
                p.name for p in staging_dir.iterdir() if p.is_dir() and not (p / "metadata.yaml").is_file()
            )
            if missing:
                self._output_lines.append(
                    f"[merge] WARNING: no data recorded for group(s) {', '.join(missing)} -- skipping"
                )
            if not part_bags:
                raise RuntimeError("No per-camera bags contained any data; nothing to merge.")

            with track("rosbag_merge"):
                self._convert_merge(part_bags, output_path)

            shutil.rmtree(staging_dir, ignore_errors=True)
            with self._lock:
                self.merge_state = "done"
            self._output_lines.append(f"[merge] Combined {len(part_bags)} recorder(s) into {output_path}")
            if self.sync_to_host_on_stop:
                self.sync_recording_to_host(str(output_path))
        except Exception as exc:  # noqa: BLE001 - surfaced via merge_error, not crashing the thread
            with self._lock:
                self.merge_state = "error"
                self.merge_error = str(exc)
            self._output_lines.append(f"[merge] ERROR: {exc} (raw per-camera bags kept at {staging_dir})")

    def _convert_merge(self, part_bags: Sequence[Path], output_path: Path) -> None:
        if output_path.exists():
            shutil.rmtree(output_path)
        config_path = output_path.parent / f".{output_path.name}.convert.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            yaml.safe_dump({"output_bags": [{"uri": str(output_path), "storage_id": "sqlite3", "all": True}]})
            if yaml is not None
            else f'output_bags:\n  - uri: "{output_path}"\n    storage_id: sqlite3\n    all: true\n'
        )
        cmd = ["ros2", "bag", "convert"]
        for bag in part_bags:
            cmd += ["-i", str(bag)]
        cmd += ["-o", str(config_path)]
        env = os.environ.copy()
        env["ROS_DOMAIN_ID"] = str(self.ros_domain_id)
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        config_path.unlink(missing_ok=True)
        if result.returncode != 0:
            raise RuntimeError(f"ros2 bag convert failed (exit {result.returncode}): {result.stdout[-2000:]}")

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
            catalog = self.topic_catalog
            output_lines = list(self._output_lines)
            recording = bool(self.processes) or self._image_writer_active
            pids = {group: process.pid for group, process in self.processes.items()}
            return {
                "recording": recording,
                "pid": next(iter(pids.values())) if pids else None,
                "pids": pids,
                "output_path": self.output_path,
                "started_at": self.started_at,
                "topics": list(self.current_topics),
                "topic_catalog": catalog,
                "recent_output": output_lines,
                "merge_state": self.merge_state,
                "merge_error": self.merge_error,
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

    # Regexes for fine-grained COLMAP sub-progress
    _RE_FEATURE = re.compile(r'Processed file \[(\d+)/(\d+)\]')
    _RE_MATCH   = re.compile(r'Matching image \[(\d+)/(\d+)\]')
    # Sub-progress fractions within the COLMAP step:
    #   0.00–0.45  feature extraction
    #   0.45–0.70  feature matching
    #   0.70–0.95  mapper
    #   0.95–1.00  post (model_converter, tum conversion, summary)
    _COLMAP_FEATURE_END  = 0.45
    _COLMAP_MATCH_START  = 0.45
    _COLMAP_MATCH_END    = 0.70
    _COLMAP_MAPPER_START = 0.70
    _COLMAP_MAPPER_END   = 0.95

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
        self._sub_progress: float = 0.0   # 0.0–1.0 within current step
        self._colmap_phase: str = ""       # "feature" | "matching" | "mapper" | "post"
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
                "sub_progress": round(self._sub_progress, 4),
                "run_name": self._run_name,
                "log_tail": list(self._log[-30:]),
                "result": dict(self._result),
            }

    @staticmethod
    def _stream_name(image_topic: str) -> str:
        known = ("infra1", "infra2", "color", "depth", "fisheye")
        for part in reversed(image_topic.rstrip("/").split("/")):
            if part in known:
                return part
        return "image"

    def start(
        self,
        bag_name: str,
        run_name: str,
        vio_topic: str,
        image_topic_str: str,
        output_hz: float = 5.0,
        camera_params: str = "",
    ) -> None:
        with self._lock:
            if self._state == "running":
                raise RuntimeError("Optimization already running")
            bag_path = self.project_root / "rosbags" / bag_name
            if not bag_path.exists():
                raise ValueError(f"Bag not found: {bag_name}")
            hz_label = str(int(output_hz)) if output_hz == int(output_hz) else str(output_hz).replace(".", "p")
            stream = self._stream_name(image_topic_str)
            self._result = {
                "trajectory_3d": f"/optimization-runs/{run_name}/viz/{stream}_{hz_label}hz_vs_vio100/trajectory_3d.png",
                "trajectory_2d": f"/optimization-runs/{run_name}/viz/{stream}_{hz_label}hz_vs_vio100/trajectory_2d.png",
                "colmap_log": f"/optimization-runs/{run_name}/colmap/{stream}_{hz_label}hz/colmap.log",
            }
            cmd = [
                sys.executable, "-u",  # force unbuffered stdout so step markers arrive immediately
                str(self.pipeline_script),
                "--bag", str(bag_path),
                "--name", run_name,
                "--vio-topic", vio_topic,
                "--image-topic", image_topic_str,
                "--colmap-runner", "local",
                "--output-hz", str(output_hz),
                "--make-plots", "false",
                "--overwrite", "true",
                *(["--camera-params", camera_params] if camera_params else []),
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
            self._sub_progress = 0.0
            self._colmap_phase = ""
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
                self._sub_progress = 0.0
                self._colmap_phase = ""

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
                        self._sub_progress = 0.0
                        self._colmap_phase = ""
                        break
                self._update_sub_progress(line)
        return_code = process.wait()
        success = return_code == 0
        with self._lock:
            if self._process is process:
                self._process = None
                self._state = "done" if success else "error"
                if success:
                    self._step = 4
                    self._sub_progress = 1.0
        if self._on_finished:
            try:
                self._on_finished(success)
            except Exception:
                pass

    def _update_sub_progress(self, line: str) -> None:
        """Parse COLMAP log lines to update fine-grained sub_progress within step 3."""
        if self._step != 3:
            return

        # Detect COLMAP sub-phase transitions
        if "$ colmap feature_extractor" in line or "$ nice" in line and "feature_extractor" in line:
            self._colmap_phase = "feature"
            self._sub_progress = 0.0
            return
        if "$ colmap sequential_matcher" in line or "$ nice" in line and "sequential_matcher" in line:
            self._colmap_phase = "matching"
            self._sub_progress = self._COLMAP_MATCH_START
            return
        if "$ colmap mapper" in line or "$ nice" in line and "colmap mapper" in line:
            self._colmap_phase = "mapper"
            self._sub_progress = self._COLMAP_MAPPER_START
            return
        if "$ colmap model_converter" in line or "COLMAP 重建完成" in line or "完成 COLMAP CLI" in line:
            self._colmap_phase = "post"
            self._sub_progress = self._COLMAP_MAPPER_END
            return

        if self._colmap_phase == "feature":
            m = self._RE_FEATURE.search(line)
            if m:
                n, total = int(m.group(1)), int(m.group(2))
                if total > 0:
                    self._sub_progress = (n / total) * self._COLMAP_FEATURE_END

        elif self._colmap_phase == "matching":
            m = self._RE_MATCH.search(line)
            if m:
                n, total = int(m.group(1)), int(m.group(2))
                if total > 0:
                    span = self._COLMAP_MATCH_END - self._COLMAP_MATCH_START
                    self._sub_progress = self._COLMAP_MATCH_START + (n / total) * span

        elif self._colmap_phase == "mapper":
            # Mapper progress is hard to quantify; nudge forward on every output line
            # so the bar visually moves, capped just below the mapper end boundary.
            nudge = 0.0005
            ceiling = self._COLMAP_MAPPER_END - nudge
            if self._sub_progress < ceiling:
                self._sub_progress = min(ceiling, self._sub_progress + nudge)
