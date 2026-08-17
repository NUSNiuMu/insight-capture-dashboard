"""Supervise offline speech recognition and apply safe recording commands."""

from __future__ import annotations

import contextlib
import json
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional


DEFAULT_START_PHRASES = ["开始录制"]
DEFAULT_STOP_PHRASES = ["结束录制", "停止录制"]


def next_restart_backoff(
    current_sec: float,
    runtime_sec: float,
    initial_sec: float,
    maximum_sec: float,
    reset_after_sec: float,
) -> float:
    """Increase crash-loop delay, resetting it after a stable worker run."""
    if runtime_sec >= reset_after_sec:
        return initial_sec
    return min(maximum_sec, max(initial_sec, current_sec * 2.0))


class VoiceRecordingController:
    def __init__(
        self,
        recording_manager,
        config: Dict[str, object],
        project_root: Path,
        log: Callable[[str], None],
    ) -> None:
        self.recording_manager = recording_manager
        self.project_root = Path(project_root)
        self.enabled = bool(config.get("enabled", False))
        self.device = str(config.get("device", "plughw:YDPI4MIC,0"))
        self.model_path = Path(str(config.get("model_path", "/opt/insight/models/vosk-cn-small")))
        self.sample_rate = max(8000, int(config.get("sample_rate", 16000)))
        self.cooldown_sec = max(0.0, float(config.get("cooldown_sec", 2.0)))
        self.restart_backoff_initial_sec = max(
            0.5, float(config.get("restart_backoff_initial_sec", 2.0))
        )
        self.restart_backoff_max_sec = max(
            self.restart_backoff_initial_sec,
            float(config.get("restart_backoff_max_sec", 60.0)),
        )
        self.restart_backoff_reset_sec = max(
            1.0, float(config.get("restart_backoff_reset_sec", 30.0))
        )
        self.min_free_ratio = max(0.0, min(1.0, float(config.get("min_free_ratio", 0.10))))
        self.start_phrases = self._phrases(config.get("start_phrases"), DEFAULT_START_PHRASES)
        self.stop_phrases = self._phrases(config.get("stop_phrases"), DEFAULT_STOP_PHRASES)
        self._log = log
        self._lock = threading.Lock()
        self._commands: "queue.Queue[Optional[tuple[str, str]]]" = queue.Queue()
        self._closed = False
        self._proc: Optional[subprocess.Popen] = None
        self._supervisor: Optional[threading.Thread] = None
        self._command_worker = threading.Thread(
            target=self._command_loop,
            daemon=True,
            name="voice_recording_commands",
        )
        self._command_worker.start()
        self._worker_state = "disabled"
        self._owned_output_path: Optional[str] = None
        self._last_event: Optional[str] = None
        self._last_phrase: Optional[str] = None
        self._grammar_phrases: list[str] = []
        self._last_error: Optional[str] = None
        self._message = "Voice recording disabled"
        if self.enabled:
            self._start_supervisor()

    @staticmethod
    def _phrases(value: object, defaults: list[str]) -> list[str]:
        if not isinstance(value, list):
            return list(defaults)
        phrases = [str(item).strip() for item in value if str(item).strip()]
        return phrases or list(defaults)

    def _start_supervisor(self) -> None:
        self._worker_state = "starting"
        self._message = "Voice recognition starting"
        self._supervisor = threading.Thread(
            target=self._supervise_worker,
            daemon=True,
            name="voice_recording_supervisor",
        )
        self._supervisor.start()

    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        supervisor = None
        proc = None
        with self._lock:
            if self._closed:
                raise RuntimeError("voice recording controller is closed")
            if self.enabled == enabled:
                return
            self.enabled = enabled
            self._last_event = None
            self._last_error = None
            if enabled:
                self._start_supervisor()
                return
            self._worker_state = "disabled"
            self._message = "Voice recording disabled"
            supervisor = self._supervisor
            self._supervisor = None
            proc = self._proc
        self._terminate(proc)
        if supervisor is not None:
            supervisor.join(timeout=3.0)
        self._log("Voice recording disabled")

    def _worker_command(self) -> list[str]:
        script_path = self.project_root / "scripts" / "voice_control_worker.py"
        command = [
            sys.executable,
            "-u",
            str(script_path),
            "--device",
            self.device,
            "--model",
            str(self.model_path),
            "--sample-rate",
            str(self.sample_rate),
            "--cooldown-sec",
            str(self.cooldown_sec),
        ]
        for phrase in self.start_phrases:
            command.extend(("--start-phrase", phrase))
        for phrase in self.stop_phrases:
            command.extend(("--stop-phrase", phrase))
        return command

    def _supervise_worker(self) -> None:
        log_path = self.project_root / "outputs" / "voice_control_worker.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        restart_delay = self.restart_backoff_initial_sec
        while True:
            with self._lock:
                if self._closed or not self.enabled:
                    return
                self._worker_state = "starting"
                self._message = "Voice recognition starting"
            with log_path.open("a", buffering=1, encoding="utf-8") as log_file:
                spawned_at = time.monotonic()
                proc = subprocess.Popen(
                    self._worker_command(),
                    stdout=subprocess.PIPE,
                    stderr=log_file,
                    text=True,
                    bufsize=1,
                )
                with self._lock:
                    if self._closed or not self.enabled:
                        self._terminate(proc)
                        return
                    self._proc = proc
                self._log(f"voice control: spawned worker pid={proc.pid} device={self.device}")
                if proc.stdout is not None:
                    for line in proc.stdout:
                        self._handle_worker_line(line)
                return_code = proc.wait()
            runtime_sec = time.monotonic() - spawned_at
            if runtime_sec >= self.restart_backoff_reset_sec:
                restart_delay = self.restart_backoff_initial_sec
            with self._lock:
                if self._proc is proc:
                    self._proc = None
                if self._closed or not self.enabled:
                    return
                self._worker_state = "restarting"
                if not self._last_error:
                    self._last_error = f"worker exited with code {return_code}"
                self._message = (
                    f"Voice recognition unavailable: {self._last_error}; "
                    f"retrying in {restart_delay:.0f}s"
                )
            self._log(
                f"voice control: worker exited ({return_code}); retrying in {restart_delay:.0f}s"
            )
            if not self._wait_for_restart(restart_delay):
                return
            restart_delay = next_restart_backoff(
                restart_delay,
                runtime_sec,
                self.restart_backoff_initial_sec,
                self.restart_backoff_max_sec,
                self.restart_backoff_reset_sec,
            )

    def _wait_for_restart(self, delay_sec: float) -> bool:
        deadline = time.monotonic() + delay_sec
        while time.monotonic() < deadline:
            with self._lock:
                if self._closed or not self.enabled:
                    return False
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        return True

    def _handle_worker_line(self, line: str) -> None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return
        event = payload.get("event")
        if event == "ready":
            with self._lock:
                self._worker_state = "listening"
                self._last_error = None
                self._grammar_phrases = [
                    str(item) for item in payload.get("grammar_phrases", [])
                ]
                self._message = "Listening for 开始录制 / 结束录制"
            self._log(f"voice control: listening on {self.device}")
        elif event == "error":
            with self._lock:
                self._last_error = str(payload.get("message") or "unknown worker error")
                self._message = f"Voice recognition unavailable: {self._last_error}"
        elif event == "command":
            command = str(payload.get("command") or "")
            phrase = str(payload.get("text") or "")
            if command in {"start", "stop"}:
                self._commands.put((command, phrase))

    def _command_loop(self) -> None:
        while True:
            item = self._commands.get()
            if item is None:
                return
            command, phrase = item
            try:
                self._apply_recording_command(command, phrase)
            except Exception as exc:  # noqa: BLE001 - keep voice control alive
                self._set_event("error", f"Voice recording error: {exc}", phrase, error=str(exc))

    def _apply_recording_command(self, command: str, phrase: str = "") -> None:
        with self._lock:
            if not self.enabled:
                return
        status = self.recording_manager.status()
        recording = bool(status.get("recording"))
        output_path = status.get("output_path")
        with self._lock:
            owned_output_path = self._owned_output_path

        if command == "stop":
            if not recording:
                self._set_event("ignored_idle", "Already idle; voice stop ignored", phrase)
                return
            if not owned_output_path or output_path != owned_output_path:
                self._set_event(
                    "ignored_manual_recording",
                    "Manual recording is active; voice stop ignored",
                    phrase,
                )
                return
            self._set_message("Stopping voice recording", phrase)
            self.recording_manager.stop()
            with self._lock:
                self._owned_output_path = None
            self._set_event("stopped", "Voice recording stopped", phrase)
            return

        if command != "start":
            raise ValueError(f"Unknown voice command: {command}")
        if recording:
            self._set_event("ignored_active", "Recording is already active", phrase)
            return
        with self._lock:
            self._owned_output_path = None
        if self.recording_manager.merge_state in {"merging", "finalizing"}:
            self._set_event("blocked_merge", "Previous recording is still stopping", phrase)
            return
        free_ratio = self._free_ratio(self.recording_manager.rosbag_root)
        if free_ratio is None:
            self._set_event("blocked_disk_unknown", "Recording disk space is unavailable", phrase)
            return
        if free_ratio < self.min_free_ratio:
            self._set_event(
                "blocked_disk_low",
                f"Recording disk has only {free_ratio * 100:.1f}% free",
                phrase,
            )
            return

        bag_name = time.strftime("voice_record_%Y%m%d_%H%M%S")
        self._set_message("Starting voice recording", phrase)
        started = self.recording_manager.start(topics=None, bag_name=bag_name)
        started_path = started.get("output_path")
        with self._lock:
            self._owned_output_path = str(started_path) if started_path else None
        self._set_event("started", "Voice recording started", phrase)

    @staticmethod
    def _free_ratio(path: Path) -> Optional[float]:
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            return None
        return usage.free / usage.total if usage.total else None

    def _set_message(self, message: str, phrase: str = "") -> None:
        with self._lock:
            self._message = message
            self._last_phrase = phrase or self._last_phrase
            self._last_error = None
        self._log(message)

    def _set_event(
        self,
        event: str,
        message: str,
        phrase: str = "",
        *,
        error: Optional[str] = None,
    ) -> None:
        with self._lock:
            self._last_event = event
            self._message = message
            self._last_phrase = phrase or self._last_phrase
            self._last_error = error
        self._log(message)

    def status(self, manager_status: Optional[Dict[str, object]] = None) -> Dict[str, object]:
        if manager_status is None:
            manager_status = self.recording_manager.status()
        recording = bool(manager_status.get("recording"))
        output_path = manager_status.get("output_path")
        with self._lock:
            owned = bool(recording and self._owned_output_path and output_path == self._owned_output_path)
            if not self.enabled:
                state = "disabled"
            elif owned:
                state = "recording"
            elif self._last_error:
                state = "error"
            elif recording:
                state = "manual_recording"
            else:
                state = self._worker_state
            return {
                "enabled": self.enabled,
                "state": state,
                "worker_state": self._worker_state,
                "device": self.device,
                "start_phrases": list(self.start_phrases),
                "stop_phrases": list(self.stop_phrases),
                "grammar_phrases": list(self._grammar_phrases),
                "last_event": self._last_event,
                "last_phrase": self._last_phrase,
                "message": self._message,
            }

    @staticmethod
    def _terminate(proc: Optional[subprocess.Popen]) -> None:
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2.0)

    def close(self) -> None:
        supervisor = None
        proc = None
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.enabled = False
            supervisor = self._supervisor
            self._supervisor = None
            proc = self._proc
            self._proc = None
        self._terminate(proc)
        if supervisor is not None:
            supervisor.join(timeout=3.0)
        self._commands.put(None)
        self._command_worker.join(timeout=3.0)
        with contextlib.suppress(Exception):
            if proc is not None and proc.stdout is not None:
                proc.stdout.close()
