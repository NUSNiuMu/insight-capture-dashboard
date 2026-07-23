"""Recording lifecycle, crash recovery, merge, and host synchronization."""

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

from check_bag import nominal_for
from perf_tracker import track

from .topic_catalog import (
    _normalize_topics,
    _topic_group,
    build_recording_topic_catalog,
    discover_live_topics,
)

try:
    import yaml
except Exception:  # pragma: no cover - recovery degrades gracefully
    yaml = None

def _trim_startup_skew(bag_dir: Path) -> Dict[str, object]:
    """Cut the ragged startup window from a freshly-merged bag.

    Each subprocess-recorded topic (IMU/VIO/camera_info/image) begins
    capturing at a slightly different wall-clock instant -- DDS discovery
    for a fresh `ros2 bag record` process takes anywhere from ~0.1s to
    occasionally 1.5-2s depending on that camera's own timing (measured
    2026-07-14 on 192.168.19.151), so right after "Start" some topics are
    already flowing while others haven't matched their publisher yet. A
    per-topic rate check reads that stagger as "loss" even though nothing
    was ever dropped -- there's just no data to lose because that camera's
    recorder wasn't listening yet.

    Fix: find the latest of every frame topic's own first-message time
    (the slowest-starting camera/stream), then delete every frame-topic
    row before that instant. What's left is either complete from its own
    true start or entirely absent -- never "started but already missing
    its first N samples". Also rewrites metadata.yaml's starting_time /
    duration / per-topic message_count so an explicitly requested
    `check_bag.py --fast` metadata estimate reports the same corrected
    picture. The default integrity check verifies individual header stamps.

    Latched/one-shot topics (tf_static -- nominal_for() returns None,
    they're not in check_bag.NOMINAL_HZ) are left untouched: they're not
    frame streams, and dropping their only sample to align with a camera
    topic would lose real static data for no completeness benefit. The
    bag-level starting_time is computed from frame topics only for the
    same reason -- otherwise an untouched tf_static sample published
    before any camera connected would drag the reported start time back
    to the exact skew this function exists to remove.

    Capped at MAX_TRIM_NS (2s): normal per-camera discovery jitter measured
    2026-07-14 topped out around 1.8s, so a skew beyond 2s means something
    actually wrong (a stalled/wedged camera, not ordinary startup variance)
    -- silently eating an unbounded amount of the front of the recording to
    paper over that would hide a real problem instead of just smoothing a
    harmless one. Past the cap, the still-lagging topic(s) keep whatever
    real gap they have and it surfaces normally in check_bag/integrity.
    """
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


# Crash-safe sqlite pragmas (WAL) for every recording writer; without this
# a power cut mid-recording leaves malformed .db3 files. See the yaml for
# details. Shared by the `ros2 bag record` subprocesses here and the
# in-process image writers (multi_camera_dashboard_web.py).

STORAGE_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "rosbag_storage_sqlite3.yaml"

def _storage_config_args() -> List[str]:
    if STORAGE_CONFIG_PATH.is_file():
        return ["--storage-config-file", str(STORAGE_CONFIG_PATH)]
    return []


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
        self._image_header_audit: Optional[Dict[str, object]] = None
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
                    *_storage_config_args(),
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

        # Signal every per-camera recorder up front (not one-at-a-time) so
        # their shutdown/flush windows overlap instead of serializing the
        # wait across N processes. Do this before detaching the in-process
        # image writers: otherwise their queue is closed first while these
        # subprocesses keep recording IMU/VIO for another ~0.2s, which makes
        # the merged bag's whole-duration rate look as though image frames
        # were missing.
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

            trim = _trim_startup_skew(output_path)
            if self._image_header_audit is not None:
                (output_path / "image_header_audit.json").write_text(
                    json.dumps(self._image_header_audit, indent=2, sort_keys=True)
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

    # ── power-loss recovery ──────────────────────────────────────────────────

    def start_orphan_recovery(self) -> None:
        """Adopt recordings interrupted by power loss or a crash, in the
        background (reindex/salvage of multi-GB bags takes a while)."""
        threading.Thread(
            target=self._recover_orphaned_stagings, daemon=True, name="staging_recovery"
        ).start()

    def _recover_orphaned_stagings(self) -> None:
        """A graceful stop merges rosbags/_staging/<name>/ into rosbags/<name>
        and removes the staging dir. A power cut leaves the staging dir behind
        with part bags that hold data but are invisible to the bag list --
        usually without metadata.yaml (written on clean close), sometimes with
        a malformed sqlite file (recordings made before the WAL config).
        For each part: reindex if only the metadata is missing, salvage via
        `sqlite3 .recover` if the database itself is broken, then merge
        whatever survived into a normal bag. Data that can't be salvaged is
        left in place for manual forensics, never deleted.
        """
        staging_root = self.rosbag_root / "_staging"
        if not staging_root.is_dir():
            return
        for staging_dir in sorted(p for p in staging_root.iterdir() if p.is_dir()):
            if staging_dir.name.endswith(".leftover"):
                continue  # already partially recovered on an earlier boot
            with self._lock:
                if self._staging_dir == staging_dir:
                    continue  # active recording, not an orphan
            try:
                self._recover_one_staging(staging_dir)
            except Exception as exc:  # noqa: BLE001 - one bad orphan must not stop the rest
                self._recovery_log(f"{staging_dir.name}: recovery failed, leaving data in place: {exc}")

    def _recover_one_staging(self, staging_dir: Path) -> None:
        part_dirs = sorted(
            p for p in staging_dir.iterdir() if p.is_dir() and list(p.glob("*.db3"))
        )
        if not part_dirs:
            self._recovery_log(f"{staging_dir.name}: no bag data at all, removing empty leftover")
            shutil.rmtree(staging_dir, ignore_errors=True)
            return
        self._recovery_log(f"{staging_dir.name}: adopting interrupted recording ({len(part_dirs)} part bags)")
        good_parts: List[Path] = []
        for part in part_dirs:
            if (part / "metadata.yaml").is_file() or self._reindex_part(part):
                good_parts.append(part)
                continue
            if self._salvage_part(part) and self._reindex_part(part):
                good_parts.append(part)
            else:
                self._recovery_log(f"{staging_dir.name}/{part.name}: unrecoverable, leaving for forensics")
        if not good_parts:
            self._recovery_log(f"{staging_dir.name}: nothing recoverable; staging dir kept")
            return
        output_path = self.rosbag_root / staging_dir.name
        if output_path.exists():
            output_path = self.rosbag_root / f"{staging_dir.name}_recovered_{time.strftime('%Y%m%d_%H%M%S')}"
        try:
            self._convert_merge(good_parts, output_path)
        except Exception:
            # A single poisoned part can segfault `ros2 bag convert` (observed
            # with one salvaged image bag, exit -11). Probe each part alone and
            # merge only the ones convert can actually read; the poisoned parts
            # stay behind in staging.
            self._recovery_log(f"{staging_dir.name}: merged convert failed; probing parts individually")
            probed = [p for p in good_parts if self._part_converts_cleanly(p)]
            dropped = [p.name for p in good_parts if p not in probed]
            if dropped:
                self._recovery_log(f"{staging_dir.name}: excluding poisoned part(s): {', '.join(dropped)}")
            if not probed:
                self._recovery_log(f"{staging_dir.name}: no part survives conversion; staging dir kept")
                return
            good_parts = probed
            self._convert_merge(good_parts, output_path)
        if len(good_parts) == len(part_dirs):
            shutil.rmtree(staging_dir, ignore_errors=True)
        else:
            # Partial salvage: keep the unrecovered parts for forensics but
            # drop the merged ones, and mark the dir so the next boot's scan
            # does not adopt the remainder again (each pass would mint yet
            # another _recovered_ bag from the same leftovers).
            for part in good_parts:
                shutil.rmtree(part, ignore_errors=True)
            staging_dir.rename(staging_dir.with_name(f"{staging_dir.name}.leftover"))
        self._recovery_log(
            f"{staging_dir.name}: recovered {len(good_parts)}/{len(part_dirs)} part bags -> {output_path.name}"
        )

    def _part_converts_cleanly(self, part: Path) -> bool:
        probe_dir = part.parent / f".{part.name}.convert_probe"
        shutil.rmtree(probe_dir, ignore_errors=True)
        try:
            self._convert_merge([part], probe_dir)
            return True
        except Exception:
            return False
        finally:
            shutil.rmtree(probe_dir, ignore_errors=True)

    def _reindex_part(self, part: Path) -> bool:
        env = os.environ.copy()
        env["ROS_DOMAIN_ID"] = str(self.ros_domain_id)
        result = subprocess.run(
            ["ros2", "bag", "reindex", "-s", "sqlite3", str(part)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
        )
        return result.returncode == 0 and (part / "metadata.yaml").is_file()

    def _salvage_part(self, part: Path) -> bool:
        """Rebuild a malformed sqlite database from its readable pages.

        Streams `sqlite3 old .recover | sqlite3 new` (the dump can be GBs,
        so no shell and no buffering the SQL in memory), then swaps the
        rebuilt file in. The corrupt original is only deleted after the
        rebuilt one opens and contains messages.
        """
        db_files = sorted(part.glob("*.db3"))
        if not db_files:
            return False
        source = db_files[0]
        rebuilt = source.with_suffix(".db3.rebuilt")
        rebuilt.unlink(missing_ok=True)
        dump = subprocess.Popen(["sqlite3", str(source), ".recover"], stdout=subprocess.PIPE)
        load = subprocess.Popen(["sqlite3", str(rebuilt)], stdin=dump.stdout)
        dump.stdout.close()
        load.wait()
        dump.wait()
        try:
            import sqlite3 as _sqlite3

            conn = _sqlite3.connect(f"file:{rebuilt}?mode=ro", uri=True)
            messages = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
            conn.close()
        except Exception:
            messages = 0
        if messages <= 0:
            rebuilt.unlink(missing_ok=True)
            return False
        # Clear stale WAL/journal companions along with the corrupt db --
        # they belong to the old file and would poison the rebuilt one.
        for leftover in part.glob(f"{source.name}-*"):
            leftover.unlink(missing_ok=True)
        source.unlink()
        rebuilt.rename(source)
        self._recovery_log(f"{part.parent.name}/{part.name}: salvaged {messages} messages from malformed database")
        return True

    def _recovery_log(self, message: str) -> None:
        line = f"[recovery] {message}"
        self._output_lines.append(line)
        print(line, flush=True)

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
