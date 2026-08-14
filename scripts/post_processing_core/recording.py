"""Recording lifecycle, crash recovery, and merge."""

import contextlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, Sequence, Set

from post_processing_core.integrity import nominal_for
from perf_tracker import track

from .topic_catalog import (
    _normalize_topics,
    _topic_group,
    build_recording_topic_catalog,
    discover_live_topics,
)
from .recovery import RecordingRecovery
from .network_audit import (
    capture_network_snapshot,
    compare_network_snapshots,
    format_network_audit,
)
try:
    import yaml
except Exception:  # pragma: no cover - recovery degrades gracefully
    yaml = None

def _trim_startup_skew(bag_dir: Path) -> Dict[str, object]:
    """Align frame-topic starts, preserve latched topics, and cap trimming at 2s."""
    MAX_TRIM_NS = 2_000_000_000
    metadata_path = bag_dir / "metadata.yaml"
    info = yaml.safe_load(metadata_path.read_text())["rosbag2_bagfile_information"]
    db_path = bag_dir / info["relative_file_paths"][0]

    conn = sqlite3.connect(str(db_path))
    try:
        topic_ids = {name: tid for tid, name in conn.execute("SELECT id, name FROM topics")}
        frame_topic_ids = [tid for name, tid in topic_ids.items() if nominal_for(name) is not None]
        if not frame_topic_ids:
            return {"trimmed_ns": 0}

        placeholders = ",".join("?" * len(frame_topic_ids))
        first_by_topic = dict(conn.execute(
            f"SELECT topic_id, MIN(timestamp) FROM messages WHERE topic_id IN ({placeholders}) "
            "GROUP BY topic_id",
            frame_topic_ids,
        ))
        if not first_by_topic:
            return {"trimmed_ns": 0}
        # Only topics that actually captured something count toward the
        # sync point -- a camera stream with zero messages is a dead-camera
        # failure, a different problem trimming can't and shouldn't mask.
        sync_point_ns = max(first_by_topic.values())
        old_min_ns = min(first_by_topic.values())
        if sync_point_ns <= old_min_ns:
            return {"trimmed_ns": 0}  # every frame topic already started together

        trim_point_ns = min(sync_point_ns, old_min_ns + MAX_TRIM_NS)
        capped = trim_point_ns < sync_point_ns

        conn.execute(
            f"DELETE FROM messages WHERE timestamp < ? AND topic_id IN ({placeholders})",
            [trim_point_ns, *frame_topic_ids],
        )
        conn.commit()

        counts = dict(conn.execute(
            "SELECT t.name, COUNT(*) FROM messages m JOIN topics t ON t.id = m.topic_id GROUP BY t.name"
        ))
        new_min_ns = conn.execute(
            f"SELECT MIN(timestamp) FROM messages WHERE topic_id IN ({placeholders})", frame_topic_ids
        ).fetchone()[0]
        new_max_ns = conn.execute("SELECT MAX(timestamp) FROM messages").fetchone()[0]
    finally:
        conn.close()

    for entry in info["topics_with_message_count"]:
        entry["message_count"] = counts.get(entry["topic_metadata"]["name"], 0)
    info["message_count"] = sum(counts.values())
    info["starting_time"]["nanoseconds_since_epoch"] = int(new_min_ns)
    info["duration"]["nanoseconds"] = int(new_max_ns - new_min_ns)
    for file_entry in info.get("files") or []:
        file_entry["starting_time"]["nanoseconds_since_epoch"] = int(new_min_ns)
        file_entry["duration"]["nanoseconds"] = int(new_max_ns - new_min_ns)
        file_entry["message_count"] = info["message_count"]

    metadata_path.write_text(yaml.dump(
        {"rosbag2_bagfile_information": info}, sort_keys=False, default_flow_style=False, width=1_000_000
    ))
    return {
        "trimmed_ns": int(trim_point_ns - old_min_ns),
        "sync_point_ns": int(sync_point_ns),
        "capped": capped,
        "residual_skew_ns": int(sync_point_ns - trim_point_ns) if capped else 0,
    }


# Shared crash-tolerant SQLite settings for subprocess and in-process writers.

STORAGE_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "rosbag_storage_sqlite3.yaml"
QOS_OVERRIDE_PATH = Path(__file__).resolve().parents[2] / "config" / "rosbag_qos_overrides.yaml"

def _storage_config_args() -> List[str]:
    if STORAGE_CONFIG_PATH.is_file():
        return ["--storage-config-file", str(STORAGE_CONFIG_PATH)]
    return []


def _qos_override_args() -> List[str]:
    if QOS_OVERRIDE_PATH.is_file():
        return ["--qos-profile-overrides-path", str(QOS_OVERRIDE_PATH)]
    return []


class RecordingManager:
    def __init__(
        self,
        raw_config: Dict,
        ros_domain_id: int,
        rosbag_root: Path,
        max_cache_size: int,
        default_topics: Sequence[str],
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
        self.publisher_checker = publisher_checker
        # Reuse dashboard image subscriptions; tests may fall back to subprocesses.
        self._image_topics: Set[str] = set(_normalize_topics(image_topics or []))
        self._start_image_recording = start_image_recording
        self._stop_image_recording = stop_image_recording
        self._image_writer_active = False
        self._image_header_audit: Optional[Dict[str, object]] = None
        self._network_snapshot_start: Optional[Dict[str, object]] = None
        self._network_audit: Optional[Dict[str, object]] = None
        self._recording_manifest: Optional[Dict[str, object]] = None
        # Group recorder processes by camera namespace.
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
        self._recording_completed_callbacks: List[Callable[[Path], None]] = []
        self.merge_state: str = "idle"  # idle | merging | done | error
        self.merge_error: Optional[str] = None
        self.merge_timings: Dict[str, object] = {}
        self._last_topic_refresh_monotonic: float = 0.0
        self._recovery_service = RecordingRecovery(self)

    def add_recording_completed_callback(self, callback: Callable[[Path], None]) -> None:
        """Register lightweight work to enqueue after a merged bag is durable."""
        with self._lock:
            self._recording_completed_callbacks.append(callback)

    def _notify_recording_completed(self, output_path: Path) -> None:
        with self._lock:
            callbacks = list(self._recording_completed_callbacks)
        for callback in callbacks:
            try:
                callback(output_path)
            except Exception as exc:  # noqa: BLE001 - completion must remain successful
                self._output_lines.append(f"[post] WARNING: completion callback failed: {exc}")


    def _cleanup_if_exited_unlocked(self) -> None:
        self.processes = {
            group: process for group, process in self.processes.items() if process.poll() is None
        }

    def is_recording(self) -> bool:
        """Cheap hot-path status for image workers; avoids building API JSON."""
        with self._lock:
            return bool(self.processes) or self._image_writer_active

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
            with contextlib.suppress(Exception):
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
                raise RuntimeError(
                    f"Previous recording is still {self.merge_state} -- wait for it to finish."
                )

            timestamp = time.strftime("%Y%m%d_%H%M%S")
            if bag_name:
                safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in bag_name.strip())
                name = safe or f"insight_record_{timestamp}"
            else:
                name = f"insight_record_{timestamp}"
            output_path = self.rosbag_root / name
            staging_dir = self.rosbag_root / "_staging" / name
            staging_dir.mkdir(parents=True, exist_ok=True)
            network_snapshot_start = capture_network_snapshot()

            if self._start_image_recording is not None:
                image_topics = [t for t in selected_topics if t in self._image_topics]
                other_topics = [t for t in selected_topics if t not in self._image_topics]
            else:
                image_topics = []
                other_topics = list(selected_topics)

            groups: Dict[str, List[str]] = {}
            for topic in other_topics:
                groups.setdefault(_topic_group(topic, self.raw_config), []).append(topic)

            image_writer_active = False
            if image_topics:
                # Keep large camera streams on independent writers.
                topic_output_paths = {
                    topic: str(staging_dir / f"_images_{_topic_group(topic, self.raw_config)}")
                    for topic in image_topics
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
                    *_storage_config_args(),
                    *_qos_override_args(),
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
            self._image_header_audit = None
            self._network_snapshot_start = network_snapshot_start
            self._network_audit = None
            self.output_path = str(output_path)
            self.started_at = time.time()
            self.current_topics = list(selected_topics)
            self._recording_manifest = {
                "version": 1,
                "bag_name": name,
                "selected_topics": list(selected_topics),
                "started_at_epoch_s": self.started_at,
            }
            self._output_lines.clear()
            self.merge_state = "idle"
            self.merge_error = None
            self.merge_timings = {}
        return self.status()

    def stop(self, timeout_sec: float = 8.0) -> Dict[str, object]:
        stop_started = time.perf_counter()
        with self._lock:
            self._cleanup_if_exited_unlocked()
            processes = dict(self.processes)
            staging_dir = self._staging_dir
            output_path = self.output_path
            image_writer_active = self._image_writer_active
            network_snapshot_start = self._network_snapshot_start
            network_snapshot_end = capture_network_snapshot()
            self._image_writer_active = False
            if not processes and not image_writer_active:
                return self.status()

        if network_snapshot_start is not None:
            network_audit = compare_network_snapshots(
                network_snapshot_start, network_snapshot_end
            )
            self._network_audit = network_audit
            self._output_lines.append(f"[network] {format_network_audit(network_audit)}")
        if self._recording_manifest is not None:
            stopped_at = time.time()
            self._recording_manifest.update({
                "stopped_at_epoch_s": stopped_at,
                "duration_s": round(max(0.0, stopped_at - float(self.started_at or stopped_at)), 3),
                "image_header_audit": "image_header_audit.json" if image_writer_active else None,
                "network_audit": "recording_network_audit.json" if self._network_audit else None,
            })

        # Stop subprocesses together before detaching image writers.
        for process in processes.values():
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGINT)
            except ProcessLookupError:
                pass

        if image_writer_active and self._stop_image_recording is not None:
            try:
                result = self._stop_image_recording()
                dropped = int(result.get("dropped", 0)) if result else 0
                if dropped:
                    self._output_lines.append(f"[_images] WARNING: dropped {dropped} message(s) (writer queue full)")
                self._image_header_audit = result.get("image_header_audit") if result else None
                if self._image_header_audit:
                    audit_topics = self._image_header_audit.get("topics", {})
                    verdict = "PASS" if self._image_header_audit.get("ok") else "FAIL"
                    details = ", ".join(
                        f"{topic}: {item.get('frames', 0)} frames, "
                        f"{item.get('missing', 0)} missing, "
                        f"{item.get('writer_queue_dropped', 0)} writer drops"
                        for topic, item in sorted(audit_topics.items())
                    )
                    self._output_lines.append(f"[_images] live header audit {verdict} -- {details}")
            except Exception as exc:  # noqa: BLE001 - surfaced via output log, not fatal to stop()
                self._output_lines.append(f"[_images] ERROR stopping writer: {exc}")
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
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=3.0)

        with self._lock:
            self.processes = {}
            self.merge_state = "merging"
            self.merge_error = None
            self.merge_timings = {
                "recorder_stop_sec": round(time.perf_counter() - stop_started, 3)
            }
        self._output_lines.append(
            f"[stop] Recorder shutdown completed in {self.merge_timings['recorder_stop_sec']:.2f}s"
        )

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
                merge_result = self._convert_merge(part_bags, output_path)

            if merge_result.get("trim_applied"):
                trim = dict(merge_result.get("trim") or {"trimmed_ns": 0})
            else:
                trim_started = time.perf_counter()
                trim = _trim_startup_skew(output_path)
                merge_result.setdefault("timings", {})["trim_sec"] = round(
                    time.perf_counter() - trim_started, 3
                )
            with self._lock:
                self.merge_timings.update(
                    {
                        "method": merge_result.get("method", "unknown"),
                        **dict(merge_result.get("timings") or {}),
                    }
                )
            self._output_lines.append(
                f"[merge] {self.merge_timings['method']} completed in "
                f"{float(self.merge_timings.get('total_sec', 0.0)):.2f}s"
            )
            if self._image_header_audit is not None:
                (output_path / "image_header_audit.json").write_text(
                    json.dumps(self._image_header_audit, indent=2, sort_keys=True)
                )
            if self._network_audit is not None:
                (output_path / "recording_network_audit.json").write_text(
                    json.dumps(self._network_audit, indent=2, sort_keys=True)
                )
            if self._recording_manifest is not None:
                (output_path / "recording_manifest.json").write_text(
                    json.dumps(self._recording_manifest, indent=2, sort_keys=True)
                )
            if trim["trimmed_ns"] > 0:
                self._output_lines.append(
                    f"[merge] Trimmed {trim['trimmed_ns'] / 1e9:.2f}s of unsynced camera-startup "
                    "skew from the front of the bag (recorders began at slightly different instants)"
                )
            if trim.get("capped"):
                total_skew_s = (trim["trimmed_ns"] + trim["residual_skew_ns"]) / 1e9
                self._output_lines.append(
                    f"[merge] WARNING: startup skew was {total_skew_s:.2f}s, beyond the 2s trim cap -- "
                    "a camera took unusually long to start; the remaining "
                    f"{trim['residual_skew_ns'] / 1e9:.2f}s gap is real and will show as loss, not silently cut"
                )

            shutil.rmtree(staging_dir, ignore_errors=True)
            self._output_lines.append(f"[merge] Combined {len(part_bags)} recorder(s) into {output_path}")
            with self._lock:
                self.merge_state = "done"
            self._notify_recording_completed(output_path)
        except Exception as exc:  # noqa: BLE001 - surfaced via merge_error, not crashing the thread
            with self._lock:
                self.merge_state = "error"
                self.merge_error = str(exc)
            self._output_lines.append(f"[merge] ERROR: {exc} (raw per-camera bags kept at {staging_dir})")

    # ── power-loss recovery ──────────────────────────────────────────────────

    def start_orphan_recovery(self) -> None:
        """Recover interrupted staging bags in the background."""
        threading.Thread(
            target=self._recover_orphaned_stagings, daemon=True, name="staging_recovery"
        ).start()

    def _recover_orphaned_stagings(self) -> None:
        self._recovery_service._recover_orphaned_stagings()


    def _recover_one_staging(self, staging_dir: Path) -> None:
        self._recovery_service._recover_one_staging(staging_dir)


    def _part_converts_cleanly(self, part: Path) -> bool:
        return self._recovery_service._part_converts_cleanly(part)


    def _reindex_part(self, part: Path) -> bool:
        return self._recovery_service._reindex_part(part)


    def _salvage_part(self, part: Path) -> bool:
        return self._recovery_service._salvage_part(part)


    def _recovery_log(self, message: str) -> None:
        self._recovery_service._recovery_log(message)


    def _convert_merge(
        self, part_bags: Sequence[Path], output_path: Path
    ) -> Dict[str, object]:
        return self._recovery_service.merge_recording_parts(part_bags, output_path)


    def status(self) -> Dict[str, object]:
        with self._lock:
            self._cleanup_if_exited_unlocked()
            catalog = self.topic_catalog
            output_lines = list(self._output_lines)
            recording = bool(self.processes) or self._image_writer_active
            return {
                "recording": recording,
                "output_path": self.output_path,
                "topic_catalog": catalog,
                "recent_output": output_lines,
                "network_audit": self._network_audit,
                "merge_state": self.merge_state,
                "merge_error": self.merge_error,
                "merge_timings": dict(self.merge_timings),
            }
