"""Background process manager for offline hand-pose extraction."""

from __future__ import annotations

import importlib.util
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from .schema import METHODS, safe_child


class HandPoseManager:
    """Run one hand-pose extraction job without blocking the web server."""

    _PROGRESS = re.compile(
        r"^HANDPOSE_PROGRESS\s+(\d+)\s+(\d+)\s+(\d+)"
    )
    _DONE = re.compile(r"^HANDPOSE_DONE\s+(\d+)\s+(\d+)\s+(\d+)")
    _MAX_LOG_LINES = 60
    _WILOR_MODEL_FILES = (
        "wilor_final.ckpt",
        "detector.pt",
        "MANO_RIGHT.pkl",
        "mano_mean_params.npz",
    )

    def __init__(
        self,
        project_root: Path,
        rosbag_root: Path,
        output_root: Optional[Path] = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.rosbag_root = rosbag_root.resolve()
        self.output_root = (
            output_root or self.project_root / "outputs" / "handpose"
        ).resolve()
        self._lock = threading.Lock()
        self._process: Optional[subprocess.Popen] = None
        self._state = "idle"
        self._bag_name = ""
        self._method = ""
        self._processed_frames = 0
        self._detected_frames = 0
        self._total_frames = 0
        self._started_at = 0.0
        self._finished_at = 0.0
        self._error = ""
        self._log = []
        self._stop_requested = False

    def capabilities(self) -> Dict[str, object]:
        wilor_missing = [
            name
            for name in ("wilor_mini", "torch", "rosbags", "cv2")
            if importlib.util.find_spec(name) is None
        ]
        wilor_model_dir = self._wilor_model_dir()
        if wilor_model_dir is None:
            wilor_missing.append("bundled WiLoR model files")
        return {
            "methods": {
                "wilor": {
                    "available": not wilor_missing,
                    "missing": wilor_missing,
                    "coordinate_space": "camera",
                },
            },
            "input_root": str(self.rosbag_root),
            "output_root": str(self.output_root),
        }

    def status(self) -> Dict[str, object]:
        with self._lock:
            payload = {
                "state": self._state,
                "bag_name": self._bag_name,
                "method": self._method,
                "processed_frames": self._processed_frames,
                "detected_frames": self._detected_frames,
                "total_frames": self._total_frames,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "log_tail": list(self._log[-30:]),
            }
            if self._error:
                payload["error"] = self._error
            if self._bag_name and self._method:
                result_path = self.result_path(
                    self._bag_name, self._method, require_file=False
                )
                payload["result_ready"] = result_path.is_file()
                payload["result_url"] = self.result_url(
                    self._bag_name, self._method
                )
                preview_path = result_path.with_name("preview.mp4")
                payload["preview_ready"] = preview_path.is_file()
                if preview_path.is_file():
                    payload["preview_url"] = self.preview_url(
                        self._bag_name, self._method
                    )
            return payload

    def list_results(self) -> list:
        entries = []
        if not self.output_root.exists():
            return entries
        for bag_dir in sorted(self.output_root.iterdir(), reverse=True):
            if not bag_dir.is_dir():
                continue
            for method in METHODS:
                result = bag_dir / method / "result.json"
                if not result.is_file():
                    continue
                entries.append(
                    {
                        "bag_name": bag_dir.name,
                        "method": method,
                        "size_bytes": result.stat().st_size,
                        "updated_at": result.stat().st_mtime,
                        "result_url": self.result_url(bag_dir.name, method),
                        "preview_ready": result.with_name("preview.mp4").is_file(),
                        "preview_url": self.preview_url(bag_dir.name, method),
                    }
                )
        return entries

    def start(self, bag_name: str, method: str) -> None:
        if method not in METHODS:
            raise ValueError(f"Unknown method '{method}'")
        bag_path = safe_child(self.rosbag_root, bag_name)
        if not bag_path.is_dir() or not (bag_path / "metadata.yaml").is_file():
            raise ValueError(f"Rosbag '{bag_name}' was not found")
        capabilities = self.capabilities()["methods"][method]
        if not capabilities["available"]:
            missing = ", ".join(capabilities["missing"])
            raise RuntimeError(f"{method} is unavailable: missing {missing}")

        with self._lock:
            if self._process is not None:
                raise RuntimeError("A hand-pose job is already running")

        result_dir = safe_child(self.output_root, bag_name) / method
        result_dir.mkdir(parents=True, exist_ok=True)
        result_path = result_dir / "result.json"
        pending_path = result_dir / "result.pending.json"
        pending_path.unlink(missing_ok=True)
        script = Path(__file__).with_name(f"extract_{method}.py")
        command = [
            sys.executable,
            "-u",
            str(script),
            str(bag_path),
            str(pending_path),
        ]
        model_dir = self._wilor_model_dir()
        if model_dir is None:
            raise RuntimeError("WiLoR model files are unavailable")
        command.extend(["--model-dir", str(model_dir)])

        environment = os.environ.copy()
        scripts_root = str(Path(__file__).resolve().parents[1])
        current_pythonpath = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = (
            scripts_root
            if not current_pythonpath
            else f"{scripts_root}{os.pathsep}{current_pythonpath}"
        )
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=str(self.project_root),
            env=environment,
            start_new_session=True,
        )
        with self._lock:
            self._process = process
            self._state = "running"
            self._bag_name = bag_name
            self._method = method
            self._processed_frames = 0
            self._detected_frames = 0
            self._total_frames = 0
            self._started_at = time.time()
            self._finished_at = 0.0
            self._error = ""
            self._log = []
            self._stop_requested = False
        threading.Thread(
            target=self._monitor,
            args=(process, pending_path, result_path),
            daemon=True,
            name="handpose_monitor",
        ).start()

    def stop(self) -> None:
        with self._lock:
            process = self._process
            if process is not None:
                self._stop_requested = True
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
                self._finished_at = time.time()
                self._stop_requested = False

    def result_path(
        self, bag_name: str, method: str, *, require_file: bool = True
    ) -> Path:
        if method not in METHODS:
            raise ValueError("Unknown hand-pose method")
        path = safe_child(self.output_root, bag_name) / method / "result.json"
        if require_file and not path.is_file():
            raise FileNotFoundError("Hand-pose result not found")
        return path

    def preview_path(self, bag_name: str, method: str) -> Path:
        path = self.result_path(bag_name, method, require_file=False).with_name(
            "preview.mp4"
        )
        if not path.is_file():
            raise FileNotFoundError("Hand-pose preview not found")
        return path

    @staticmethod
    def result_url(bag_name: str, method: str) -> str:
        from urllib.parse import urlencode

        return "/api/handpose/result?" + urlencode(
            {"bag_name": bag_name, "method": method}
        )

    @staticmethod
    def preview_url(bag_name: str, method: str) -> str:
        from urllib.parse import urlencode

        return "/api/handpose/preview?" + urlencode(
            {"bag_name": bag_name, "method": method}
        )

    def _wilor_model_dir(self) -> Optional[Path]:
        configured = os.environ.get("HANDPOSE_WILOR_MODEL_DIR", "").strip()
        candidates = [
            Path(configured) if configured else None,
            self.project_root / "data" / "models" / "handpose" / "wilor",
        ]
        for candidate in candidates:
            if candidate is None:
                continue
            model_dir = candidate.resolve()
            pretrained_dir = model_dir / "pretrained_models"
            if all(
                (pretrained_dir / filename).is_file()
                for filename in self._WILOR_MODEL_FILES
            ):
                return model_dir
        return None

    def _monitor(
        self,
        process: subprocess.Popen,
        pending_path: Path,
        result_path: Path,
    ) -> None:
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.rstrip()
            progress = self._PROGRESS.match(line) or self._DONE.match(line)
            with self._lock:
                if progress:
                    self._processed_frames = int(progress.group(1))
                    self._detected_frames = int(progress.group(2))
                    self._total_frames = int(progress.group(3))
                else:
                    self._log.append(line)
                    if len(self._log) > self._MAX_LOG_LINES:
                        self._log = self._log[-self._MAX_LOG_LINES :]
        return_code = process.wait()
        with self._lock:
            if self._process is not process:
                return
            self._process = None
            self._finished_at = time.time()
            if self._stop_requested:
                self._state = "idle"
                self._stop_requested = False
            elif return_code == 0 and pending_path.is_file():
                pending_path.replace(result_path)
                self._state = "done"
            else:
                self._state = "error"
                self._error = (
                    self._log[-1]
                    if self._log
                    else f"Extractor exited with code {return_code}"
                )
