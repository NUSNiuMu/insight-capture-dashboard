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


_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass
class _UmiExportItem:
    bag_name: str
    bag_path: Path
    dataset_name: str
    output_path: Path
    status: str = "pending"
    result: Optional[Dict[str, object]] = None
    error: Optional[str] = None


@dataclass
class _UmiExportJob:
    bag_names: list[str]
    image_size: Optional[int]
    camera_names: list[str]
    items: list[_UmiExportItem]
    status: str = "running"
    stage: str = "starting"
    current_bag: str = ""
    completed_bags: int = 0
    total_frames: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    error: Optional[str] = None
    cancelled: bool = False


class UmiExportManager:
    """Export each selected rosbag to an independent UMI dataset."""

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
                "bag_names": job.bag_names,
                "current_bag": job.current_bag,
                "completed_bags": job.completed_bags,
                "bag_count": len(job.bag_names),
                "total_frames": job.total_frames,
                "started_at": job.started_at,
                "image_mode": (
                    "original" if job.image_size is None else str(job.image_size)
                ),
                "camera_names": job.camera_names,
                "items": [self._item_payload(item) for item in job.items],
            }
            if job.finished_at:
                payload["finished_at"] = job.finished_at
            if job.error:
                payload["error"] = job.error
            return payload

    def start(
        self,
        bag_names: list[str],
        image_mode: str = "original",
        camera_names: Optional[list[str]] = None,
    ) -> Dict[str, object]:
        image_mode = str(image_mode).strip().lower()
        if image_mode == "original":
            image_size = None
        elif image_mode in {"224", "384"}:
            image_size = int(image_mode)
        else:
            raise ValueError("Image resolution must be original, 224, or 384.")
        if not bag_names:
            raise ValueError("Select at least one rosbag.")
        if len(bag_names) > 1000:
            raise ValueError("Too many rosbags selected.")
        resolved_bags = [self._bag_path(name) for name in bag_names]
        if len(set(bag_names)) != len(bag_names):
            raise ValueError("Duplicate rosbag names are not allowed.")
        if camera_names is None:
            camera_names = ["insight3_a", "insight3_b", "insight9_a"]
        if not camera_names:
            raise ValueError("Select at least one camera.")
        camera_names = [
            self._validate_name(name, "camera name") for name in camera_names
        ]
        if len(camera_names) > 10:
            raise ValueError("Too many cameras selected.")
        if len(set(camera_names)) != len(camera_names):
            raise ValueError("Duplicate camera names are not allowed.")
        items = [
            _UmiExportItem(
                bag_name=bag_name,
                bag_path=bag_path,
                dataset_name=f"{bag_name}_umi",
                output_path=(
                    self.output_root
                    / f"{bag_name}_umi"
                    / f"{bag_name}_umi.zarr.zip"
                ),
            )
            for bag_name, bag_path in zip(bag_names, resolved_bags)
        ]
        with self._lock:
            if self._current_job and self._current_job.status == "running":
                raise RuntimeError("A UMI export job is already running.")
            job = _UmiExportJob(
                bag_names=list(bag_names),
                image_size=image_size,
                camera_names=camera_names,
                items=items,
                started_at=time.time(),
            )
            self._current_job = job
        threading.Thread(
            target=self._worker,
            args=(job,),
            daemon=True,
            name="umi_export",
        ).start()
        return self.status()

    def stop(self) -> None:
        with self._lock:
            process = self._process
            if self._current_job is not None:
                self._current_job.cancelled = True
        if process is not None and process.poll() is None:
            process.terminate()

    def _worker(self, job: _UmiExportJob) -> None:
        completed_frames = 0
        successful_items = 0
        failed_items = 0
        for item_index, item in enumerate(job.items):
            with self._lock:
                if job.cancelled:
                    break
                item.status = "running"
                job.stage = "starting"
                job.current_bag = item.bag_name
                job.total_frames = completed_frames
            try:
                result = self._export_item(job, item, completed_frames)
                with self._lock:
                    item.result = result
                    item.status = "done"
                completed_frames += int(result.get("total_frames", 0))
                successful_items += 1
            except Exception as exc:
                with self._lock:
                    item.status = "error"
                    item.error = str(exc)
                try:
                    item.output_path.parent.rmdir()
                except OSError:
                    pass
                failed_items += 1
            finally:
                with self._lock:
                    job.completed_bags = item_index + 1
                    job.total_frames = completed_frames
                    self._process = None

        with self._lock:
            job.finished_at = time.time()
            if job.cancelled:
                job.status = "error"
                job.stage = "error"
                job.error = "UMI export stopped."
            elif failed_items and successful_items:
                job.status = "partial"
                job.stage = "done"
                job.error = f"{failed_items} of {len(job.items)} rosbags failed."
            elif failed_items:
                job.status = "error"
                job.stage = "error"
                job.error = f"All {failed_items} rosbags failed."
            else:
                job.status = "done"
                job.stage = "done"

    def _export_item(
        self, job: _UmiExportJob, item: _UmiExportItem, completed_frames: int
    ) -> Dict[str, object]:
        command = [
            "/usr/bin/python3",
            str(self._SCRIPT),
            str(item.bag_path),
            "--output",
            str(item.output_path),
            "--camera-config",
            str(self.project_root / "config" / "cameras.json"),
            "--calibration",
            str(self.project_root / "config" / "gripper_calibration.json"),
            "--image-size",
            "original" if job.image_size is None else str(job.image_size),
        ]
        for camera_name in job.camera_names:
            command.extend(("--camera", camera_name))
        output_tail = deque(maxlen=12)
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
                    job.stage = fields[3]
                    job.current_bag = item.bag_name
                    job.total_frames = completed_frames + int(fields[5])
        if process.wait() != 0:
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
        manifest_path = item.output_path.with_name(
            f"{item.dataset_name}.manifest.json"
        )
        return json.loads(manifest_path.read_text(encoding="utf-8"))

    def _item_payload(self, item: _UmiExportItem) -> Dict[str, object]:
        manifest_path = item.output_path.with_name(
            f"{item.dataset_name}.manifest.json"
        )
        config_path = item.output_path.with_name(f"{item.dataset_name}.umi.yaml")
        payload: Dict[str, object] = {
            "bag_name": item.bag_name,
            "dataset_name": item.dataset_name,
            "status": item.status,
            "output_path": str(item.output_path.relative_to(self.project_root)),
            "manifest_path": str(manifest_path.relative_to(self.project_root)),
            "config_path": str(config_path.relative_to(self.project_root)),
        }
        if item.result is not None:
            payload["result"] = item.result
        if item.error:
            payload["error"] = item.error
        return payload

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
