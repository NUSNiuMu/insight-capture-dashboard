"""Background service for offline rosbag gripper extraction."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional


_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass
class _GripperJob:
    bag_name: str
    camera_name: str
    topic: str
    output_path: Path
    status: str = "running"
    processed_frames: int = 0
    both_detected_frames: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    result: Optional[Dict[str, object]] = None
    error: Optional[str] = None


class GripperExtractionManager:
    """Run at most one gripper extractor and expose its current state."""

    _SCRIPT = Path(__file__).resolve().parents[1] / "gripper_extract.py"

    def __init__(self, project_root: Path, rosbag_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.rosbag_root = rosbag_root.resolve()
        self.output_root = self.project_root / "outputs" / "gripper"
        self.calibration_path = self.project_root / "config" / "gripper_calibration.json"
        self._lock = threading.Lock()
        self._current_job: Optional[_GripperJob] = None
        self._process: Optional[subprocess.Popen[str]] = None

    def status(self) -> Dict[str, object]:
        with self._lock:
            job = self._current_job
            if job is None:
                return {"status": "idle"}
            payload: Dict[str, object] = {
                "status": job.status,
                "bag_name": job.bag_name,
                "camera_name": job.camera_name,
                "topic": job.topic,
                "processed_frames": job.processed_frames,
                "both_detected_frames": job.both_detected_frames,
                "started_at": job.started_at,
            }
            if job.finished_at:
                payload["finished_at"] = job.finished_at
            if job.result is not None:
                payload["result"] = {
                    "source": job.result.get("source", {}),
                    "calibration": job.result.get("calibration", {}),
                    "summary": job.result.get("summary", {}),
                }
                payload["result_url"] = (
                    "/api/gripper-extraction/result?"
                    f"bag_name={job.bag_name}&camera_name={job.camera_name}"
                )
            if job.error:
                payload["error"] = job.error
            return payload

    def start(
        self,
        bag_name: str,
        camera_name: str,
        topic: str = "",
        *,
        require_calibration: bool = True,
    ) -> Dict[str, object]:
        bag_path = self._bag_path(bag_name)
        camera_name = self._validate_name(camera_name, "camera name")
        if topic and not topic.startswith("/"):
            raise ValueError("topic must start with '/'")
        output_path = self.output_root / bag_name / f"{camera_name}.json"
        with self._lock:
            if self._current_job and self._current_job.status == "running":
                raise RuntimeError("A gripper extraction job is already running.")
            job = _GripperJob(
                bag_name=bag_name,
                camera_name=camera_name,
                topic=topic,
                output_path=output_path,
                started_at=time.time(),
            )
            self._current_job = job
        threading.Thread(
            target=self._worker,
            args=(job, bag_path, require_calibration),
            daemon=True,
            name="gripper_extract",
        ).start()
        return self.status()

    def result_path(self, bag_name: str, camera_name: str) -> Path:
        self._bag_path(bag_name)
        camera_name = self._validate_name(camera_name, "camera name")
        path = self.output_root / bag_name / f"{camera_name}.json"
        if not path.is_file():
            raise FileNotFoundError("Gripper extraction result not found.")
        return path

    def stop(self) -> None:
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            process.terminate()

    def _worker(
        self,
        job: _GripperJob,
        bag_path: Path,
        require_calibration: bool,
    ) -> None:
        command = [
            "/usr/bin/python3",
            str(self._SCRIPT),
            str(bag_path),
            "--camera", job.camera_name,
            "--output", str(job.output_path),
            "--calibration", str(self.calibration_path),
        ]
        if job.topic:
            command.extend(["--topic", job.topic])
        if require_calibration:
            command.append("--require-calibration")
        output_tail = deque(maxlen=8)
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=os.environ.copy(),
            )
            with self._lock:
                self._process = process
            assert process.stdout is not None
            for line in process.stdout:
                output_tail.append(line.rstrip())
                fields = line.strip().split()
                if len(fields) >= 3 and fields[0] == "GRIPPER_PROGRESS":
                    with self._lock:
                        job.processed_frames = int(fields[1])
                        job.both_detected_frames = int(fields[2])
            return_code = process.wait()
            if return_code != 0:
                message = "\n".join(output_tail).strip()
                raise RuntimeError(message or f"extractor exited with code {return_code}")
            result = json.loads(job.output_path.read_text(encoding="utf-8"))
            summary = result.get("summary", {})
            with self._lock:
                job.processed_frames = int(summary.get("total_frames", 0))
                job.both_detected_frames = int(summary.get("both_detected_frames", 0))
                job.result = result
                job.status = "done"
                job.finished_at = time.time()
        except Exception as exc:
            with self._lock:
                job.status = "error"
                job.error = str(exc)
                job.finished_at = time.time()
        finally:
            with self._lock:
                self._process = None

    def _bag_path(self, bag_name: str) -> Path:
        bag_name = self._validate_name(bag_name, "bag name")
        path = (self.rosbag_root / bag_name).resolve()
        if not path.is_relative_to(self.rosbag_root) or not path.is_dir():
            raise FileNotFoundError("Rosbag not found.")
        return path

    @staticmethod
    def _validate_name(value: str, label: str) -> str:
        value = str(value).strip()
        if not value or not _SAFE_NAME.fullmatch(value) or value in {".", ".."}:
            raise ValueError(f"Invalid {label}.")
        return value
