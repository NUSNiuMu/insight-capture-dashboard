"""Single MCAP recording lifecycle, publishing and crash recovery."""

import contextlib
import concurrent.futures
import json
import os
import pty
import re
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, Sequence, Set


from insight_capture.legacy.composite_bag import MANIFEST_NAME, read_metadata
from .storage import probe_recording_root
from .recorder import (
    _normalize_topics,
    build_recording_topic_catalog,
    discover_live_topics,
)
from .recovery import RECORDING_TARGET_NAME, RecordingRecovery
from .network_audit import (
    capture_network_snapshot,
    compare_network_snapshots,
    format_network_audit,
)
QOS_OVERRIDE_PATH = Path(__file__).resolve().parents[3] / "config" / "rosbag_qos_overrides.yaml"


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
        drain_image_recording: Optional[Callable[[], None]] = None,
        recording_rmw_implementation: str = "rmw_cyclonedds_cpp",
        storage_status: Optional[Dict[str, object]] = None,
        storage_resolver: Optional[Callable[[], tuple[Path, Dict[str, object]]]] = None,
        storage_browse_roots: Optional[Sequence[Path]] = None,
    ) -> None:
        self.raw_config = raw_config
        self.ros_domain_id = int(ros_domain_id)
        self.rosbag_root = rosbag_root.resolve()
        self.rosbag_root.mkdir(parents=True, exist_ok=True)
        self.max_cache_size = int(max_cache_size)
        self.recording_rmw_implementation = str(recording_rmw_implementation or "")
        self.storage_status = dict(storage_status or {
            "active_path": str(self.rosbag_root), "using_fallback": False
        })
        self._storage_resolver = storage_resolver
        browse_roots = storage_browse_roots or [self.rosbag_root]
        self._storage_browse_roots: List[Path] = []
        for browse_root in browse_roots:
            resolved = Path(browse_root).resolve()
            if resolved not in self._storage_browse_roots:
                self._storage_browse_roots.append(resolved)
        if self.rosbag_root not in self._storage_browse_roots:
            self._storage_browse_roots.append(self.rosbag_root)
        self._manual_storage_selected = False
        self._storage_changed_callbacks: List[Callable[[Path], None]] = []
        self.default_topics = _normalize_topics(default_topics)
        self.publisher_checker = publisher_checker
        # Reuse dashboard image subscriptions for a continuity audit.
        self._image_topics: Set[str] = set(_normalize_topics(image_topics or []))
        self._start_image_recording = start_image_recording
        self._stop_image_recording = stop_image_recording
        self._drain_image_recording = drain_image_recording
        self._image_writer_active = False
        self._image_header_audit: Optional[Dict[str, object]] = None
        self._network_snapshot_start: Optional[Dict[str, object]] = None
        self._network_audit: Optional[Dict[str, object]] = None
        self._recording_manifest: Optional[Dict[str, object]] = None
        # One native rosbag2 process owns every selected MCAP topic. Dashboard
        # image callbacks provide only an independent live continuity audit.
        self.processes: Dict[str, subprocess.Popen] = {}
        self._process_exit_codes: Dict[str, int] = {}
        self._staging_dir: Optional[Path] = None
        self.output_path: Optional[str] = None
        self.started_at: Optional[float] = None
        self.recording_mode: Optional[str] = None
        self.current_topics: List[str] = []
        self.topic_catalog = build_recording_topic_catalog(raw_config, [], self.default_topics)
        self._output_lines: Deque[str] = deque(maxlen=200)
        self._recorder_loss_lines: List[str] = []
        self._recorder_ready = threading.Event()
        self._recorder_resumed = threading.Event()
        self._all_recorder_topics_subscribed = threading.Event()
        self._last_recorder_subscription_monotonic = 0.0
        self._recorder_subscribed_topics: Set[str] = set()
        self._recorder_control_fds: Dict[str, int] = {}
        self._stdout_threads: List[threading.Thread] = []
        self._lock = threading.Lock()
        self._merge_thread: Optional[threading.Thread] = None
        self._recording_completed_callbacks: List[Callable[[Path], None]] = []
        self.merge_state: str = "idle"  # idle | finalizing | merging | done | error
        self.merge_error: Optional[str] = None
        self.merge_timings: Dict[str, object] = {}
        self.start_timings: Dict[str, object] = {}
        self._last_topic_refresh_monotonic: float = 0.0
        self._recovery_service = RecordingRecovery(self)

    def add_recording_completed_callback(self, callback: Callable[[Path], None]) -> None:
        """Register lightweight work to enqueue after a merged bag is durable."""
        with self._lock:
            self._recording_completed_callbacks.append(callback)

    def add_storage_changed_callback(self, callback: Callable[[Path], None]) -> None:
        """Update consumers that resolve bags relative to the active root."""
        with self._lock:
            self._storage_changed_callbacks.append(callback)

    def storage_browse_roots(self) -> List[Path]:
        """Return the stable roots that may contain visible recordings."""

        with self._lock:
            return list(self._storage_browse_roots)

    def _refresh_recording_storage_unlocked(self) -> None:
        # Once failover occurs, remain on NVMe until restart. Switching roots
        # back and forth would make just-recorded bags disappear from the UI.
        if self._manual_storage_selected:
            probe_error = probe_recording_root(self.rosbag_root)
            if probe_error:
                raise RuntimeError(
                    f"Selected recording directory is unavailable: {probe_error}"
                )
            return
        if self._storage_resolver is None or self.storage_status.get("using_fallback"):
            return
        active, status = self._storage_resolver()
        active = active.resolve()
        changed = active != self.rosbag_root
        self.rosbag_root = active
        self.storage_status = dict(status)
        if not changed:
            return
        self._output_lines.append(
            f"[storage] switched recording root to {active}: "
            f"{self.storage_status.get('fallback_reason') or 'storage selection changed'}"
        )
        for callback in list(self._storage_changed_callbacks):
            try:
                callback(active)
            except Exception as exc:  # noqa: BLE001 - capture remains the priority
                self._output_lines.append(
                    f"[storage] WARNING: root-change callback failed: {exc}"
                )

    def _storage_browse_root_for(self, path: Path) -> Optional[Path]:
        resolved = path.resolve()
        for root in self._storage_browse_roots:
            if resolved == root or root in resolved.parents:
                return root
        return None

    def browse_recording_directories(self, path: Optional[str] = None) -> Dict[str, object]:
        """List safe server-side destination folders for the recording UI."""
        with self._lock:
            current_root = self.rosbag_root
            recording = bool(self.processes) or self._image_writer_active
            roots = list(self._storage_browse_roots)

        target = Path(path).resolve() if path else current_root
        containing_root = self._storage_browse_root_for(target)
        if containing_root is None:
            raise ValueError("Recording directory is outside the allowed storage locations.")
        if not target.is_dir():
            raise ValueError(f"Recording directory does not exist: {target}")

        directories = []
        try:
            children = sorted(target.iterdir(), key=lambda child: child.name.casefold())
        except OSError as exc:
            raise ValueError(f"Cannot read recording directory {target}: {exc}") from exc
        for child in children:
            if child.name.startswith(".") or child.name in {"_outputs", "_staging", "_distributed"}:
                continue
            try:
                resolved = child.resolve()
                if not resolved.is_dir() or self._storage_browse_root_for(resolved) is None:
                    continue
                # A rosbag is an output item, not another destination folder.
                if (resolved / "metadata.yaml").is_file():
                    continue
                directories.append({
                    "name": child.name,
                    "path": str(resolved),
                    "writable": os.access(resolved, os.W_OK | os.X_OK),
                })
            except OSError:
                continue

        parent = target.parent if target != containing_root else None
        available_roots = []
        for root in roots:
            available_roots.append({
                "name": root.name or str(root),
                "path": str(root),
                "available": root.is_dir(),
                "current": current_root == root or root in current_root.parents,
            })
        return {
            "path": str(target),
            "parent": str(parent) if parent is not None else None,
            "directories": directories,
            "roots": available_roots,
            "current_path": str(current_root),
            "selectable": os.access(target, os.W_OK | os.X_OK),
            "recording": recording,
        }

    def select_recording_root(self, path: str) -> Dict[str, object]:
        """Select and probe an allowed recording root while capture is idle."""
        target = Path(path).resolve()
        if self._storage_browse_root_for(target) is None:
            raise ValueError("Recording directory is outside the allowed storage locations.")
        if not target.is_dir():
            raise ValueError(f"Recording directory does not exist: {target}")

        callbacks: List[Callable[[Path], None]] = []
        with self._lock:
            self._cleanup_if_exited_unlocked()
            if self.processes or self._image_writer_active:
                raise RuntimeError("Cannot change the recording directory while recording.")
            if self.merge_state in {"merging", "finalizing"}:
                raise RuntimeError("Cannot change the recording directory while finalizing.")
            probe_error = probe_recording_root(target)
            if probe_error:
                raise RuntimeError(
                    f"Recording directory is not writable or durable: {probe_error}"
                )
            changed = target != self.rosbag_root
            configured_value = str(self.storage_status.get("configured_path") or "").strip()
            configured_root = Path(configured_value).resolve() if configured_value else target
            manually_using_fallback = not (
                target == configured_root or configured_root in target.parents
            )
            previous_fallback_reason = self.storage_status.get("fallback_reason")
            manual_fallback_reason = None
            if manually_using_fallback:
                manual_fallback_reason = (
                    previous_fallback_reason or "manually selected recording directory"
                )
            self.rosbag_root = target
            self._manual_storage_selected = True
            self.storage_status.update({
                "active_path": str(target),
                "manually_selected": True,
                "using_fallback": manually_using_fallback,
                "fallback_reason": manual_fallback_reason,
            })
            if changed:
                self._output_lines.append(
                    f"[storage] recording directory selected: {target}"
                )
                callbacks = list(self._storage_changed_callbacks)

        for callback in callbacks:
            try:
                callback(target)
            except Exception as exc:  # noqa: BLE001 - storage selection already succeeded
                self._output_lines.append(
                    f"[storage] WARNING: root-change callback failed: {exc}"
                )
        return self.status()

    def _notify_recording_completed(self, output_path: Path) -> None:
        with self._lock:
            callbacks = list(self._recording_completed_callbacks)
        for callback in callbacks:
            try:
                callback(output_path)
            except Exception as exc:  # noqa: BLE001 - completion must remain successful
                self._output_lines.append(f"[post] WARNING: completion callback failed: {exc}")


    def _cleanup_if_exited_unlocked(self) -> None:
        running: Dict[str, subprocess.Popen] = {}
        for group, process in self.processes.items():
            return_code = process.poll()
            if return_code is None:
                running[group] = process
            else:
                self._process_exit_codes[group] = int(return_code)
        self.processes = running

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
                stripped = line.rstrip()
                self._output_lines.append(f"[{label}] {stripped}")
                if "Recording..." in stripped or "Waiting for recording:" in stripped:
                    self._recorder_ready.set()
                if "Resuming recording." in stripped:
                    self._recorder_resumed.set()
                if "All requested topics are subscribed." in stripped:
                    self._all_recorder_topics_subscribed.set()
                if "Subscribed to topic" in stripped:
                    self._last_recorder_subscription_monotonic = time.monotonic()
                    match = re.search(r"Subscribed to topic '([^']+)'", stripped)
                    if match:
                        self._recorder_subscribed_topics.add(match.group(1))
                if re.search(
                    r"(?:cache buffers lost messages|dropping message on topic)",
                    stripped,
                    flags=re.IGNORECASE,
                ):
                    self._recorder_loss_lines.append(stripped)
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

    def probe_topic_payloads(
        self, topics: Sequence[str], *, timeout_sec: float = 5.0
    ) -> Dict[str, object]:
        """Read one header from each topic so advertised-only streams fail closed."""

        selected = _normalize_topics(topics)
        env = os.environ.copy()
        env["ROS_DOMAIN_ID"] = str(self.ros_domain_id)
        env["ROS2CLI_NO_DAEMON"] = "1"
        # Use the Dashboard's default RMW for the temporary probes. The native
        # recorder may deliberately use a different implementation that is
        # installed only inside the release container.
        env.pop("RMW_IMPLEMENTATION", None)

        def probe(topic: str) -> tuple[str, Dict[str, object]]:
            started = time.monotonic()
            try:
                completed = subprocess.run(
                    [
                        "ros2",
                        "topic",
                        "echo",
                        "--once",
                        topic,
                        "--field",
                        "header",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=max(0.2, float(timeout_sec)),
                    env=env,
                    check=False,
                )
                ok = completed.returncode == 0
                error = (
                    ""
                    if ok
                    else (completed.stderr or "topic returned no payload").strip()
                )
            except subprocess.TimeoutExpired:
                ok = False
                error = f"no payload within {float(timeout_sec):g}s"
            except OSError as exc:
                ok = False
                error = str(exc)
            return topic, {
                "ok": ok,
                "elapsed_sec": round(time.monotonic() - started, 3),
                "error": error,
            }

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, min(4, len(selected)))
        ) as executor:
            results = dict(executor.map(probe, selected))
        missing = [topic for topic in selected if not results[topic]["ok"]]
        return {"ok": not missing, "topics": results, "missing": missing}

    def start(
        self,
        topics: Optional[Sequence[str]] = None,
        bag_name: Optional[str] = None,
        recording_mode: str = "capture",
        output_subdirectory: Optional[str] = None,
    ) -> Dict[str, object]:
        start_started = time.perf_counter()
        selected_topics = self.default_topics if topics is None else _normalize_topics(topics)
        if not selected_topics:
            raise ValueError("No topics selected for recording.")
        with self._lock:
            lock_acquired = time.perf_counter()
            self._cleanup_if_exited_unlocked()
            if self.processes or self._image_writer_active:
                raise RuntimeError("Recording is already running.")
            if self.merge_state in {"merging", "finalizing"}:
                raise RuntimeError(
                    f"Previous recording is still {self.merge_state} -- wait for it to finish."
                )
            self._output_lines.clear()
            storage_started = time.perf_counter()
            self._refresh_recording_storage_unlocked()
            storage_finished = time.perf_counter()

            timestamp = time.strftime("%Y%m%d_%H%M%S")
            if bag_name:
                safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in bag_name.strip())
                name = safe or f"insight_record_{timestamp}"
            else:
                name = f"insight_record_{timestamp}"
            safe_subdirectory = ""
            if output_subdirectory:
                candidate = str(output_subdirectory).strip()
                if not re.fullmatch(r"[0-9A-Za-z_-]+", candidate):
                    raise ValueError("Recording subdirectory must be a single safe task-set name.")
                safe_subdirectory = candidate
            output_root = self.rosbag_root / safe_subdirectory if safe_subdirectory else self.rosbag_root
            output_root.mkdir(parents=True, exist_ok=True)
            output_path = output_root / name
            staging_dir = self.rosbag_root / "_staging" / name
            staging_dir.parent.mkdir(parents=True, exist_ok=True)
            if staging_dir.exists():
                raise RuntimeError(f"Recording staging path already exists: {staging_dir}")
            image_writer_active = False
            audit_topics = set(selected_topics).intersection(self._image_topics)

            env = os.environ.copy()
            env["ROS_DOMAIN_ID"] = str(self.ros_domain_id)
            if self.recording_rmw_implementation:
                env["RMW_IMPLEMENTATION"] = self.recording_rmw_implementation
            processes: Dict[str, subprocess.Popen] = {}
            stdout_threads: List[threading.Thread] = []
            control_fds: Dict[str, int] = {}
            self._recorder_loss_lines = []
            self._recorder_ready.clear()
            self._recorder_resumed.clear()
            self._all_recorder_topics_subscribed.clear()
            self._last_recorder_subscription_monotonic = 0.0
            self._recorder_subscribed_topics = set()
            setup_finished = time.perf_counter()

            def abort_started_processes() -> None:
                for started_process in processes.values():
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(os.getpgid(started_process.pid), signal.SIGINT)
                for started_process in processes.values():
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        started_process.wait(timeout=3.0)
                for control_fd in control_fds.values():
                    with contextlib.suppress(OSError):
                        os.close(control_fd)

            spawn_started = time.perf_counter()
            master_fd, slave_fd = pty.openpty()
            try:
                process = subprocess.Popen(
                    [
                        "ros2", "bag", "record", "--storage", "mcap",
                        "--output", str(staging_dir),
                        "--max-cache-size", str(self.max_cache_size),
                        *_qos_override_args(), "--start-paused", *selected_topics,
                    ],
                    stdin=slave_fd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                    env=env,
                )
            except Exception:
                os.close(master_fd)
                raise
            finally:
                os.close(slave_fd)
            control_fds["single"] = master_fd
            processes["single"] = process
            thread = threading.Thread(
                target=self._drain_stdout,
                args=("single", process),
                daemon=True,
                name="rosbag_record_stdout_single",
            )
            thread.start()
            stdout_threads.append(thread)
            spawn_finished = time.perf_counter()

            if not self._recorder_ready.wait(timeout=10.0):
                abort_started_processes()
                raise RuntimeError("Native MCAP recorder did not become ready within 10 seconds.")
            if safe_subdirectory:
                staging_dir.mkdir(parents=True, exist_ok=True)
                (staging_dir / RECORDING_TARGET_NAME).write_text(
                    json.dumps({"output_subdirectory": safe_subdirectory}, sort_keys=True),
                    encoding="utf-8",
                )
            ready_finished = time.perf_counter()
            # CycloneDDS discovers each camera participant in a burst. Wait
            # until every selected dashboard stream has a subscription;
            # those streams prove that each active camera participant and
            # its accompanying IMU/VIO topics were discovered. A short
            # quiet tail lets the remainder of the same burst finish.
            settle_deadline = time.monotonic() + 20.0
            while time.monotonic() < settle_deadline:
                if self._all_recorder_topics_subscribed.is_set():
                    break
                last_subscription = self._last_recorder_subscription_monotonic
                required_topics_ready = audit_topics.issubset(
                    self._recorder_subscribed_topics
                )
                quiet_sec = 1.0 if audit_topics else 8.0
                if (
                    required_topics_ready
                    and last_subscription
                    and time.monotonic() - last_subscription >= quiet_sec
                ):
                    break
                time.sleep(0.1)
            else:
                abort_started_processes()
                raise RuntimeError("Native MCAP topic subscriptions did not settle.")
            discovery_finished = time.perf_counter()

            network_snapshot_start = capture_network_snapshot()
            network_snapshot_finished = time.perf_counter()
            resume_requested = network_snapshot_finished
            for control_fd in control_fds.values():
                os.write(control_fd, b" ")
            if not self._recorder_resumed.wait(timeout=5.0):
                abort_started_processes()
                raise RuntimeError("Native MCAP recorder did not resume from pause.")
            resume_confirmed = time.perf_counter()
            if self._start_image_recording is not None and audit_topics:
                try:
                    self._start_image_recording({
                        topic: str(staging_dir) for topic in audit_topics
                    })
                except Exception:
                    abort_started_processes()
                    raise
                image_writer_active = True
            audit_finished = time.perf_counter()

            self.processes = processes
            self._process_exit_codes = {}
            self._stdout_threads = stdout_threads
            self._recorder_control_fds = control_fds
            self._staging_dir = staging_dir
            self._image_writer_active = image_writer_active
            self._image_header_audit = None
            self._network_snapshot_start = network_snapshot_start
            self._network_audit = None
            self.output_path = str(output_path)
            self.started_at = time.time()
            self.recording_mode = str(recording_mode or "capture")
            self.current_topics = list(selected_topics)
            self._recording_manifest = {
                "version": 3,
                "format": "rosbag2",
                "storage_id": "mcap",
                "rmw_implementation": self.recording_rmw_implementation,
                "bag_name": name,
                "task_set_id": safe_subdirectory or None,
                "recording_mode": self.recording_mode,
                "selected_topics": list(selected_topics),
                "started_at_epoch_s": self.started_at,
                "camera_configuration": json.loads(json.dumps(self.raw_config)),
            }
            self.merge_state = "idle"
            self.merge_error = None
            self.merge_timings = {}
            state_published = time.perf_counter()
            self.start_timings = {
                "lock_wait_sec": round(lock_acquired - start_started, 4),
                "storage_check_sec": round(storage_finished - storage_started, 4),
                "setup_sec": round(setup_finished - storage_finished, 4),
                "recorder_spawn_sec": round(spawn_finished - spawn_started, 4),
                "recorder_ready_wait_sec": round(ready_finished - spawn_finished, 4),
                "recorder_ready_offset_sec": round(ready_finished - start_started, 4),
                "dds_subscription_settle_sec": round(discovery_finished - ready_finished, 4),
                "network_snapshot_sec": round(
                    network_snapshot_finished - discovery_finished, 4
                ),
                "resume_wait_sec": round(resume_confirmed - resume_requested, 4),
                "resume_requested_offset_sec": round(
                    resume_requested - start_started, 4
                ),
                "resume_confirmed_offset_sec": round(
                    resume_confirmed - start_started, 4
                ),
                "audit_enable_sec": round(audit_finished - resume_confirmed, 4),
                "state_publish_sec": round(state_published - audit_finished, 4),
                "total_sec": round(state_published - start_started, 4),
            }
            self._output_lines.append(
                f"[startup] {json.dumps(self.start_timings, sort_keys=True)}"
            )
        return self.status()

    def stop(
        self,
        timeout_sec: float = 30.0,
        *,
        expected_recording: Optional[tuple[str, str]] = None,
    ) -> Dict[str, object]:
        """Finalize the writer after checking an optional identity under its lock."""
        stop_started = time.perf_counter()
        with self._lock:
            if expected_recording is not None and expected_recording != (
                self.output_path, self.recording_mode
            ):
                raise RuntimeError("采集已变化，停止请求未执行，请刷新状态。")
            self._cleanup_if_exited_unlocked()
            processes = dict(self.processes)
            prior_exit_codes = dict(self._process_exit_codes)
            stdout_threads = list(self._stdout_threads)
            control_fds = dict(self._recorder_control_fds)
            staging_dir = self._staging_dir
            output_path = self.output_path
            image_writer_active = self._image_writer_active
            network_snapshot_start = self._network_snapshot_start
            network_snapshot_end = capture_network_snapshot()
            self._image_writer_active = False
            if not processes and not image_writer_active and not prior_exit_codes:
                return self._status_unlocked()
            self.merge_state = "finalizing"

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

        # Drain queued source frames while the native writer can still accept access units.
        image_writer_error: Optional[str] = None
        if image_writer_active and self._drain_image_recording is not None:
            try:
                self._drain_image_recording()
            except Exception as exc:
                image_writer_error = str(exc)
                self._output_lines.append(f"[images] ERROR draining encoders: {exc}")
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
                    image_writer_error = f"writer queues dropped {dropped} message(s)"
                    self._output_lines.append(f"[writer] WARNING: dropped {dropped} message(s) (writer queue full)")
                if result:
                    pending_topics = list(result.get("pending_topics") or [])
                    if pending_topics:
                        self._output_lines.append(
                            f"[writer] {len(pending_topics)} selected topic(s) remained source-silent"
                        )
                self._image_header_audit = result.get("image_header_audit") if result else None
                if self._image_header_audit:
                    audit_topics = self._image_header_audit.get("topics", {})
                    verdict = "PASS" if self._image_header_audit.get("ok") else "GAPS"
                    details = ", ".join(
                        f"{topic}: {item.get('frames', 0)} frames, "
                        f"{item.get('missing', 0)} missing, "
                        f"{item.get('writer_queue_dropped', 0)} writer drops"
                        for topic, item in sorted(audit_topics.items())
                    )
                    self._output_lines.append(
                        f"[images] dashboard subscription header audit {verdict} -- {details}"
                    )
            except Exception as exc:  # noqa: BLE001 - finalization is rejected below
                image_writer_error = str(exc)
                self._output_lines.append(f"[images] ERROR stopping live audit: {exc}")
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
        for thread in stdout_threads:
            thread.join(timeout=2.0)
        for control_fd in control_fds.values():
            with contextlib.suppress(OSError):
                os.close(control_fd)

        recorder_loss_lines = list(self._recorder_loss_lines)

        exit_codes = dict(prior_exit_codes)
        exit_codes.update({group: int(process.returncode) for group, process in processes.items()
                           if process.returncode is not None})
        failed_recorders = {
            group: return_code for group, return_code in exit_codes.items() if return_code != 0
        }

        stop_sec = round(time.perf_counter() - stop_started, 3)
        with self._lock:
            self.processes = {}
            self._recorder_control_fds = {}
            self._process_exit_codes = exit_codes
            self.merge_error = None
            self.merge_timings = {"recorder_stop_sec": stop_sec}
        self._output_lines.append(f"[stop] Recorder shutdown completed in {stop_sec:.2f}s")
        try:
            if still_running:
                raise RuntimeError(
                    f"recorder process(es) did not stop cleanly: {len(still_running)}"
                )
            if failed_recorders:
                details = ", ".join(
                    f"{group}={return_code}" for group, return_code in sorted(failed_recorders.items())
                )
                raise RuntimeError(f"recorder process failure(s): {details}")
            if recorder_loss_lines:
                raise RuntimeError(
                    "native recorder reported message loss: "
                    + " | ".join(recorder_loss_lines[-3:])
                )
            if image_writer_error:
                raise RuntimeError(f"image writer failure: {image_writer_error}")
            self._publish_single_mcap_session(
                staging_dir, Path(output_path) if output_path else None
            )
        except Exception as exc:  # noqa: BLE001 - preserve staging for forensics
            with self._lock:
                self.merge_state = "error"
                self.merge_error = str(exc)
            self._output_lines.append(
                f"[finalize] ERROR: {exc} (raw staging data kept at {staging_dir})"
            )
        return self.status()

    def _publish_single_mcap_session(
        self, staging_dir: Path, output_path: Optional[Path]
    ) -> None:
        """Atomically expose one standard rosbag2 MCAP directory."""
        if output_path is None:
            raise RuntimeError("Missing recording output path.")
        started = time.perf_counter()
        info = read_metadata(staging_dir)
        mcap_files = sorted(staging_dir.glob("*.mcap"))
        if not info or not mcap_files:
            raise RuntimeError("Single MCAP writer did not finalize a readable rosbag.")
        if len(mcap_files) != 1:
            raise RuntimeError(
                f"Single MCAP writer unexpectedly produced {len(mcap_files)} data files."
            )
        if self._image_header_audit is not None:
            (staging_dir / "image_header_audit.json").write_text(
                json.dumps(self._image_header_audit, indent=2, sort_keys=True)
            )
        if self._network_audit is not None:
            (staging_dir / "recording_network_audit.json").write_text(
                json.dumps(self._network_audit, indent=2, sort_keys=True)
            )
        manifest = dict(self._recording_manifest or {})
        manifest.update({
            "message_count": int(info.get("message_count", 0) or 0),
            "topic_count": len(info.get("topics_with_message_count") or []),
            "published_at_epoch_s": time.time(),
        })
        (staging_dir / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )
        if output_path.exists():
            raise RuntimeError(f"recording output already exists: {output_path}")
        (staging_dir / RECORDING_TARGET_NAME).unlink(missing_ok=True)
        os.replace(staging_dir, output_path)
        finalize_sec = round(time.perf_counter() - started, 3)
        with self._lock:
            self._staging_dir = None
            self.merge_state = "done"
            self.merge_timings.update({
                "method": "single_mcap_publish",
                "part_count": 1,
                "total_sec": finalize_sec,
            })
        self._output_lines.append(
            f"[finalize] Published one standard MCAP rosbag in {finalize_sec:.2f}s: "
            f"{output_path}"
        )
        self._notify_recording_completed(output_path)

    # ── power-loss recovery ──────────────────────────────────────────────────

    def start_orphan_recovery(self) -> None:
        """Recover interrupted staging bags in the background."""
        threading.Thread(
            target=self._recovery_service._recover_orphaned_stagings, daemon=True, name="staging_recovery"
        ).start()

    def status(self) -> Dict[str, object]:
        with self._lock:
            self._cleanup_if_exited_unlocked()
            return self._status_unlocked()

    def _status_unlocked(self) -> Dict[str, object]:
        return {
            "recording": bool(self.processes) or self._image_writer_active,
            "recording_mode": self.recording_mode,
            "output_path": self.output_path,
            "topic_catalog": self.topic_catalog,
            "recent_output": list(self._output_lines),
            "network_audit": self._network_audit,
            "image_header_audit": self._image_header_audit,
            "merge_state": self.merge_state,
            "merge_error": self.merge_error,
            "merge_timings": dict(self.merge_timings),
            "start_timings": dict(self.start_timings),
            "storage": dict(self.storage_status),
        }
