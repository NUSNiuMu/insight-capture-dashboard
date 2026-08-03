"""Background manager for exporting training-ready UMI datasets."""

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
from urllib.parse import quote


_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass
class _UmiExportJob:
    dataset_name: str
    bag_names: list[str]
    output_path: Path
    status: str = "running"
    stage: str = "starting"
    current_bag: str = ""
    completed_bags: int = 0
    total_frames: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    result: Optional[Dict[str, object]] = None
    error: Optional[str] = None


class UmiExportManager:
    """Run one rosbag-to-UMI export without blocking the dashboard."""

    _SCRIPT = Path(__file__).resolve().parents[1] / "umi_dataset_export.py"

    def __init__(self, project_root: Path, rosbag_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.rosbag_root = rosbag_root.resolve()
        self.output_root = self.project_root / "outputs" / "umi_datasets"
        self._lock = threading.Lock()
        self._current_job: Optional[_UmiExportJob] = None
        self._process: Optional[subprocess.Popen[str]] = None

    def status(self) -> Dict[str, object]:
        with self._lock:
            job = self._current_job
            if job is None:
                return {"status": "idle"}
            payload: Dict[str, object] = {
                "status": job.status,
                "stage": job.stage,
                "dataset_name": job.dataset_name,
                "bag_names": job.bag_names,
                "current_bag": job.current_bag,
                "completed_bags": job.completed_bags,
                "bag_count": len(job.bag_names),
                "total_frames": job.total_frames,
                "started_at": job.started_at,
            }
            if job.finished_at:
                payload["finished_at"] = job.finished_at
            if job.error:
                payload["error"] = job.error
            if job.result is not None:
                payload["result"] = job.result
                query = quote(job.dataset_name, safe="")
                payload["result_url"] = f"/api/umi-export/result?dataset_name={query}"
                payload["manifest_url"] = f"/api/umi-export/manifest?dataset_name={query}"
                payload["config_url"] = f"/api/umi-export/config?dataset_name={query}"
            return payload

    def start(self, dataset_name: str, bag_names: list[str]) -> Dict[str, object]:
        dataset_name = self._validate_name(dataset_name, "dataset name")
        if not bag_names:
            raise ValueError("Select at least one rosbag.")
        if len(bag_names) > 1000:
            raise ValueError("Too many rosbags selected.")
        resolved_bags = [self._bag_path(name) for name in bag_names]
        if len(set(bag_names)) != len(bag_names):
            raise ValueError("Duplicate rosbag names are not allowed.")
        output_path = self.output_root / f"{dataset_name}.zarr.zip"
        with self._lock:
            if self._current_job and self._current_job.status == "running":
                raise RuntimeError("A UMI export job is already running.")
            job = _UmiExportJob(
                dataset_name=dataset_name,
                bag_names=list(bag_names),
                output_path=output_path,
                started_at=time.time(),
            )
            self._current_job = job
        threading.Thread(
            target=self._worker,
            args=(job, resolved_bags),
            daemon=True,
            name="umi_export",
        ).start()
        return self.status()

    def result_path(self, dataset_name: str, *, artifact: str = "dataset") -> Path:
        dataset_name = self._validate_name(dataset_name, "dataset name")
        suffixes = {
            "dataset": ".zarr.zip",
            "manifest": ".manifest.json",
            "config": ".umi.yaml",
        }
        if artifact not in suffixes:
            raise ValueError("Invalid UMI artifact type.")
        suffix = suffixes[artifact]
        path = self.output_root / f"{dataset_name}{suffix}"
        if not path.is_file():
            raise FileNotFoundError("UMI dataset result not found.")
        return path

    def stop(self) -> None:
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            process.terminate()

    def _worker(self, job: _UmiExportJob, bag_paths: list[Path]) -> None:
        command = [
            "/usr/bin/python3",
            str(self._SCRIPT),
            *(str(path) for path in bag_paths),
            "--output",
            str(job.output_path),
            "--camera-config",
            str(self.project_root / "config" / "cameras.json"),
            "--calibration",
            str(self.project_root / "config" / "gripper_calibration.json"),
        ]
        output_tail = deque(maxlen=12)
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
                fields = line.strip().split(maxsplit=6)
                if len(fields) >= 6 and fields[0] == "UMI_PROGRESS":
                    with self._lock:
                        job.completed_bags = max(int(fields[1]) - 1, 0)
                        job.stage = fields[3]
                        job.current_bag = fields[4]
                        job.total_frames = int(fields[5])
            return_code = process.wait()
            if return_code != 0:
                error_lines = [
                    line.removeprefix("ERROR: ")
                    for line in output_tail
                    if line.startswith("ERROR: ")
                ]
                raise RuntimeError(
                    error_lines[-1]
                    if error_lines
                    else "\n".join(output_tail).strip() or "UMI exporter failed"
                )
            manifest_path = self.output_root / f"{job.dataset_name}.manifest.json"
            result = json.loads(manifest_path.read_text(encoding="utf-8"))
            with self._lock:
                job.status = "done"
                job.stage = "done"
                job.completed_bags = len(job.bag_names)
                job.total_frames = int(result.get("total_frames", 0))
                job.result = result
                job.finished_at = time.time()
        except Exception as exc:
            with self._lock:
                job.status = "error"
                job.stage = "error"
                job.error = str(exc)
                job.finished_at = time.time()
        finally:
            with self._lock:
                self._process = None

    def _bag_path(self, bag_name: str) -> Path:
        bag_name = self._validate_name(bag_name, "bag name")
        path = (self.rosbag_root / bag_name).resolve()
        if not path.is_relative_to(self.rosbag_root) or not path.is_dir():
            raise FileNotFoundError(f"Rosbag not found: {bag_name}")
        if not (path / "metadata.yaml").is_file():
            raise ValueError(f"Rosbag has no metadata.yaml: {bag_name}")
        return path

    @staticmethod
    def _validate_name(value: str, label: str) -> str:
        value = str(value).strip()
        if not value or not _SAFE_NAME.fullmatch(value) or value in {".", ".."}:
            raise ValueError(f"Invalid {label}.")
        return value
