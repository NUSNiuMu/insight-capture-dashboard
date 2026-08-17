"""Coordinate debounced hand gestures with recording lifecycle operations."""

from __future__ import annotations

import queue
import shutil
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from hand_tracking.gestures import DoubleThumbsUpLatch, classify_double_thumbs_up


class GestureRecordingController:
    def __init__(
        self,
        recording_manager,
        config: Dict[str, object],
        log: Callable[[str], None],
    ) -> None:
        self.recording_manager = recording_manager
        self.enabled = bool(config.get("enabled", False))
        self.camera = str(config.get("camera", "insight9_a"))
        self.min_keypoint_score = max(0.0, min(1.0, float(config.get("min_keypoint_score", 0.35))))
        self.min_free_ratio = max(0.0, min(1.0, float(config.get("min_free_ratio", 0.10))))
        self.snapshot_timeout_sec = max(0.05, float(config.get("snapshot_timeout_sec", 0.35)))
        self._log = log
        self._hold_sec = float(config.get("hold_sec", 0.8))
        self._release_sec = float(config.get("release_sec", 2.0))
        self._hold_gap_sec = float(config.get("hold_gap_sec", 0.15))
        self._latch = self._new_latch()
        self._lock = threading.Lock()
        self._commands: "queue.Queue[Optional[str]]" = queue.Queue()
        self._closed = False
        self._command_pending = False
        self._owned_output_path: Optional[str] = None
        self._last_event: Optional[str] = None
        self._last_error: Optional[str] = None
        self._message = "Gesture recording armed" if self.enabled else "Gesture recording disabled"
        self._last_snapshot_monotonic: Optional[float] = None
        self._worker: Optional[threading.Thread] = None
        if self.enabled:
            self._worker = self._new_worker()
            self._worker.start()

    def _new_latch(self) -> DoubleThumbsUpLatch:
        return DoubleThumbsUpLatch(
            hold_sec=self._hold_sec,
            release_sec=self._release_sec,
            hold_gap_sec=self._hold_gap_sec,
        )

    def _new_worker(self) -> threading.Thread:
        return threading.Thread(
            target=self._command_loop,
            daemon=True,
            name="gesture_recording",
        )

    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        worker_to_join: Optional[threading.Thread] = None
        worker_to_start: Optional[threading.Thread] = None
        with self._lock:
            if self._closed:
                raise RuntimeError("gesture recording controller is closed")
            if self.enabled == enabled:
                return
            self.enabled = enabled
            self._latch = self._new_latch()
            self._last_snapshot_monotonic = None
            self._last_event = None
            self._last_error = None
            self._command_pending = False
            self._message = (
                "Gesture recording armed"
                if enabled
                else "Gesture recording disabled"
            )
            if enabled:
                self._commands = queue.Queue()
                worker_to_start = self._new_worker()
                self._worker = worker_to_start
            else:
                worker_to_join = self._worker
                self._worker = None
                if worker_to_join is not None:
                    self._commands.put(None)
        if worker_to_join is not None:
            worker_to_join.join(timeout=2.0)
        if worker_to_start is not None:
            worker_to_start.start()
        self._log(self._message)

    def handle_snapshot(
        self,
        camera_name: str,
        hands: List[Dict[str, object]],
        *,
        is_live: bool,
    ) -> None:
        if not self.enabled or not is_live or camera_name != self.camera:
            return
        result = classify_double_thumbs_up(hands, self.min_keypoint_score)
        now = time.monotonic()
        with self._lock:
            if self._closed:
                return
            triggered = self._latch.update(result.active, now)
            self._last_snapshot_monotonic = now
            latch = self._latch.snapshot(now)
            if latch.phase == "armed" and not latch.active:
                if self._last_event and (
                    self._last_event.startswith("blocked")
                    or self._last_event in {"stopped", "ignored_manual_recording"}
                ):
                    self._last_event = None
                    self._message = "Gesture recording armed"
            if triggered and not self._command_pending:
                self._command_pending = True
                self._commands.put("toggle")

    def _command_loop(self) -> None:
        while True:
            try:
                command = self._commands.get(timeout=0.1)
            except queue.Empty:
                self._expire_stale_snapshot()
                continue
            if command is None:
                return
            try:
                self._toggle_recording()
            except Exception as exc:  # noqa: BLE001 - expose failures without killing control
                with self._lock:
                    self._last_error = str(exc)
                    self._message = f"Gesture recording error: {exc}"
                self._log(self._message)
            finally:
                with self._lock:
                    self._command_pending = False

    def _expire_stale_snapshot(self) -> None:
        now = time.monotonic()
        with self._lock:
            if (
                self._last_snapshot_monotonic is None
                or now - self._last_snapshot_monotonic <= self.snapshot_timeout_sec
            ):
                return
            was_armed = self._latch.armed
            self._latch.update(False, now)
            if not was_armed and self._latch.armed:
                if self._last_event and (
                    self._last_event.startswith("blocked")
                    or self._last_event in {"stopped", "ignored_manual_recording"}
                ):
                    self._last_event = None
                    self._message = "Gesture recording armed"

    def _toggle_recording(self) -> None:
        with self._lock:
            if not self.enabled:
                return
        status = self.recording_manager.status()
        recording = bool(status.get("recording"))
        output_path = status.get("output_path")
        with self._lock:
            owned_output_path = self._owned_output_path

        if recording:
            if not owned_output_path or output_path != owned_output_path:
                self._set_event(
                    "ignored_manual_recording",
                    "Manual recording is active; gesture stop ignored",
                )
                return
            self._set_message("Stopping gesture recording")
            self.recording_manager.stop()
            with self._lock:
                self._owned_output_path = None
            self._set_event("stopped", "Gesture recording stopped; release thumbs for 2 seconds")
            return

        with self._lock:
            self._owned_output_path = None
        if self.recording_manager.merge_state in {"merging", "finalizing"}:
            self._set_event("blocked_merge", "Previous recording is still stopping")
            return
        free_ratio = self._free_ratio(self.recording_manager.rosbag_root)
        if free_ratio is None:
            self._set_event("blocked_disk_unknown", "Recording disk space is unavailable")
            return
        if free_ratio < self.min_free_ratio:
            self._set_event(
                "blocked_disk_low",
                f"Recording disk has only {free_ratio * 100:.1f}% free",
            )
            return

        bag_name = time.strftime("gesture_record_%Y%m%d_%H%M%S")
        self._set_message("Starting gesture recording")
        started = self.recording_manager.start(topics=None, bag_name=bag_name)
        started_path = started.get("output_path")
        with self._lock:
            self._owned_output_path = str(started_path) if started_path else None
        self._set_event("started", "Gesture recording started; release thumbs for 2 seconds")

    @staticmethod
    def _free_ratio(path: Path) -> Optional[float]:
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            return None
        return usage.free / usage.total if usage.total else None

    def _set_message(self, message: str) -> None:
        with self._lock:
            self._message = message
            self._last_error = None
        self._log(message)

    def _set_event(self, event: str, message: str) -> None:
        with self._lock:
            self._last_event = event
            self._message = message
            self._last_error = None
        self._log(message)

    def status(self, manager_status: Optional[Dict[str, object]] = None) -> Dict[str, object]:
        now = time.monotonic()
        if manager_status is None:
            manager_status = self.recording_manager.status()
        recording = bool(manager_status.get("recording"))
        output_path = manager_status.get("output_path")
        with self._lock:
            latch = self._latch.snapshot(now)
            owned = bool(
                recording
                and self._owned_output_path
                and output_path == self._owned_output_path
            )
            if not self.enabled:
                state = "disabled"
            elif self._command_pending:
                state = "working"
            elif owned:
                state = "recording"
            elif recording:
                state = "manual_recording"
            elif self._last_error:
                state = "error"
            elif (
                self._last_event
                and self._last_event.startswith("blocked")
                and latch.phase == "release_required"
            ):
                state = self._last_event
            else:
                state = latch.phase
            return {
                "enabled": self.enabled,
                "state": state,
                "gesture_phase": latch.phase,
                "hold_progress": round(latch.hold_progress, 3),
                "release_progress": round(latch.release_progress, 3),
                "release_sec": self._latch.release_sec,
                "message": self._message,
            }

    def close(self) -> None:
        worker = None
        with self._lock:
            if self._closed:
                return
            self._closed = True
            worker = self._worker
            self._worker = None
        if worker is not None:
            self._commands.put(None)
            worker.join(timeout=2.0)
