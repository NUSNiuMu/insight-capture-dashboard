"""Background manager for exporting training-ready UMI datasets."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from insight_capture.postprocess.datasets.routing import build_ego_spec, inspect_gripper_markers
from insight_capture.legacy.umi_zarr import load_camera_specs


_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_FAILED_BAG_PREFIX = "fail_"


@dataclass
class _UmiExportItem:
    bag_name: str
    bag_path: Path
    dataset_name: str
    output_path: Path
    export_format: str
    route: str = "pending"
    route_diagnostics: Optional[Dict[str, object]] = None
    status: str = "pending"
    result: Optional[Dict[str, object]] = None
    error: Optional[str] = None
    source_failed_name: Optional[str] = None
    source_rename_error: Optional[str] = None


class _RejectedRosbagError(RuntimeError):
    """The exporter rejected a bag because its recorded data is unusable."""


@dataclass
class _UmiExportJob:
    bag_names: list[str]
    image_size: Optional[int]
    camera_names: list[str]
    episode_mode: str
    export_format: str
    task: str
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
    """Export each selected rosbag to an independent training dataset."""

    _UMI_MODULE = "insight_capture.legacy.umi_zarr"
    _LEROBOT_MODULE = "insight_capture.postprocess.datasets.lerobot"
    _EGO_LEROBOT_MODULE = "insight_capture.postprocess.datasets.ego_lerobot.cli"

    def __init__(self, project_root: Path, rosbag_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.rosbag_root = rosbag_root.resolve()
        self.umi_output_root = self.project_root / "outputs" / "umi_datasets"
        self.lerobot_output_root = (
            self.project_root / "outputs" / "lerobot_datasets"
        )
        self.umi_output_root.mkdir(parents=True, exist_ok=True)
        self.lerobot_output_root.mkdir(parents=True, exist_ok=True)
        # Container root is root-squashed on some bind-mounted workspaces. Keep
        # generated datasets manageable by the workstation user.
        self.umi_output_root.chmod(0o777)
        self.lerobot_output_root.chmod(0o777)
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
                "episode_mode": job.episode_mode,
                "export_format": job.export_format,
                "task": job.task,
                "routes": {item.bag_name: item.route for item in job.items},
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
        episode_mode: str = "bag",
        export_format: str = "umi",
        task: str = "",
    ) -> Dict[str, object]:
        image_mode = str(image_mode).strip().lower()
        if image_mode == "original":
            image_size = None
        elif image_mode in {"224", "384"}:
            image_size = int(image_mode)
        else:
            raise ValueError("Image resolution must be original, 224, or 384.")
        episode_mode = str(episode_mode).strip().lower()
        if episode_mode not in {"bag", "auto_pause"}:
            raise ValueError("Episode mode must be bag or auto_pause.")
        export_format = str(export_format).strip().lower()
        if export_format not in {"lerobot", "umi"}:
            raise ValueError("Dataset format must be lerobot or umi.")
        task = " ".join(str(task).split())
        if export_format == "lerobot" and not task:
            raise ValueError("Task instruction is required for LeRobot export.")
        if len(task) > 500:
            raise ValueError("Task instruction is too long.")
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
        items = []
        for bag_name, bag_path in zip(bag_names, resolved_bags):
            if export_format == "lerobot":
                dataset_name = f"{bag_name}_lerobot"
                output_path = self.lerobot_output_root / dataset_name
            else:
                dataset_name = f"{bag_name}_umi"
                output_path = (
                    self.umi_output_root
                    / dataset_name
                    / f"{dataset_name}.zarr.zip"
                )
            items.append(
                _UmiExportItem(
                    bag_name=bag_name,
                    bag_path=bag_path,
                    dataset_name=dataset_name,
                    output_path=output_path,
                    export_format=export_format,
                )
            )
        with self._lock:
            if self._current_job and self._current_job.status == "running":
                raise RuntimeError("A dataset export job is already running.")
            job = _UmiExportJob(
                bag_names=list(bag_names),
                image_size=image_size,
                camera_names=camera_names,
                episode_mode=episode_mode,
                export_format=export_format,
                task=task,
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
        rejected_items: list[_UmiExportItem] = []
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
                if isinstance(exc, _RejectedRosbagError):
                    rejected_items.append(item)
                if item.export_format == "umi":
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

        if not job.cancelled:
            with self._lock:
                job.stage = "quarantine"
            for item in rejected_items:
                self._quarantine_rejected_source(item)

        with self._lock:
            job.finished_at = time.time()
            if job.cancelled:
                job.status = "error"
                job.stage = "error"
                job.error = "Dataset export stopped."
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
        temporary_spec: Optional[tempfile.TemporaryDirectory[str]] = None
        module = self._UMI_MODULE
        if item.export_format == "lerobot":
            with self._lock:
                job.stage = "detect_route"
            specs = load_camera_specs(
                self.project_root / "config" / "cameras.json", job.camera_names
            )
            hand_topics = {
                spec.name: spec.image_topic for spec in specs if spec.role != "head"
            }
            diagnostics = inspect_gripper_markers(item.bag_path, hand_topics)
            item.route = str(diagnostics["route"])
            item.route_diagnostics = diagnostics
            if item.route == "umi_gripper":
                module = self._LEROBOT_MODULE
            else:
                module = self._EGO_LEROBOT_MODULE
        elif item.export_format == "umi":
            item.route = "umi_gripper"
        command = [
            "/usr/bin/python3",
            "-m", module,
            str(item.bag_path),
            "--output",
            str(item.output_path),
            "--camera-config",
            str(self.project_root / "config" / "cameras.json"),
            "--calibration",
            str(self.project_root / "config" / "gripper_calibration.json"),
            "--image-size",
            "original" if job.image_size is None else str(job.image_size),
            "--episode-mode",
            job.episode_mode,
        ]
        if item.export_format == "lerobot" and item.route == "umi_gripper":
            command.extend(
                (
                    "--task",
                    job.task,
                    "--dataset-id",
                    f"insight/{item.dataset_name}",
                )
            )
        if item.route == "ego_hand":
            temporary_spec = tempfile.TemporaryDirectory(prefix="ego_lerobot_spec_")
            spec_path = Path(temporary_spec.name) / "spec.json"
            spec_path.write_text(
                json.dumps(
                    build_ego_spec(
                        item.bag_path,
                        self.project_root / "config" / "cameras.json",
                        dataset_id=f"insight/{item.dataset_name}",
                        task=job.task,
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            command = [
                sys.executable,
                "-u",
                str(script),
                str(item.bag_path),
                str(item.output_path),
                "--spec",
                str(spec_path),
                "--camera-config",
                str(self.project_root / "config" / "cameras.json"),
            ]
            with self._lock:
                job.stage = "hand_inference"
        else:
            for camera_name in job.camera_names:
                command.extend(("--camera", camera_name))
        output_tail = deque(maxlen=12)
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=os.environ.copy(),
            umask=0,
        )
        with self._lock:
            self._process = process
        assert process.stdout is not None
        for line in process.stdout:
            output_tail.append(line.rstrip())
            fields = line.strip().split(maxsplit=6)
            if len(fields) >= 6 and fields[0] in {
                "UMI_PROGRESS",
                "DATASET_PROGRESS",
            }:
                with self._lock:
                    job.stage = fields[3]
                    job.current_bag = item.bag_name
                    job.total_frames = completed_frames + int(fields[5])
        return_code = process.wait()
        if temporary_spec is not None:
            temporary_spec.cleanup()
        if return_code != 0:
            rejected_lines = []
            for line in output_tail:
                for prefix in ("UMI_REJECTED_BAG ", "DATASET_REJECTED_BAG "):
                    if line.startswith(prefix):
                        rejected_lines.append(line.removeprefix(prefix))
            if rejected_lines:
                raise _RejectedRosbagError(rejected_lines[-1])
            error_lines = [
                line.removeprefix("ERROR: ")
                for line in output_tail
                if line.startswith("ERROR: ")
            ]
            raise RuntimeError(
                error_lines[-1]
                if error_lines
                else "\n".join(output_tail).strip() or "Dataset exporter failed"
            )
        manifest_path = self._manifest_path(item)
        result = json.loads(manifest_path.read_text(encoding="utf-8"))
        manageable_root = (
            item.output_path
            if item.export_format == "lerobot"
            else item.output_path.parent
        )
        result.setdefault(
            "size_bytes",
            sum(
                path.stat().st_size
                for path in manageable_root.rglob("*")
                if path.is_file()
            ),
        )
        result["route"] = item.route
        self._make_output_user_manageable(manageable_root)
        return result

    def _quarantine_rejected_source(self, item: _UmiExportItem) -> None:
        try:
            bag_path = item.bag_path.resolve()
            if not bag_path.is_relative_to(self.rosbag_root):
                raise RuntimeError("Rejected rosbag resolved outside rosbag root.")
            if not bag_path.is_dir():
                raise FileNotFoundError(
                    f"Rejected rosbag no longer exists: {bag_path.name}"
                )
            failed_name = f"{_FAILED_BAG_PREFIX}{bag_path.name}"
            failed_path = self.rosbag_root / failed_name
            suffix = 2
            while failed_path.exists():
                failed_name = f"{_FAILED_BAG_PREFIX}{suffix}_{bag_path.name}"
                failed_path = self.rosbag_root / failed_name
                suffix += 1
            bag_path.rename(failed_path)
            item.source_failed_name = failed_name
        except (OSError, RuntimeError) as exc:
            item.source_rename_error = str(exc)

    @staticmethod
    def _make_output_user_manageable(directory: Path) -> None:
        for path in directory.rglob("*"):
            path.chmod(0o777 if path.is_dir() else 0o666)
        directory.chmod(0o777)

    def _item_payload(self, item: _UmiExportItem) -> Dict[str, object]:
        manifest_path = self._manifest_path(item)
        config_path = item.output_path.with_name(f"{item.dataset_name}.umi.yaml")
        payload: Dict[str, object] = {
            "bag_name": item.bag_name,
            "dataset_name": item.dataset_name,
            "status": item.status,
            "output_path": str(item.output_path.relative_to(self.project_root)),
            "manifest_path": str(manifest_path.relative_to(self.project_root)),
            "export_format": item.export_format,
            "route": item.route,
        }
        if item.route_diagnostics is not None:
            payload["route_diagnostics"] = item.route_diagnostics
        if item.export_format == "umi":
            payload["config_path"] = str(config_path.relative_to(self.project_root))
        if item.result is not None:
            payload["result"] = item.result
        if item.error:
            payload["error"] = item.error
        if item.source_failed_name:
            payload["source_failed_name"] = item.source_failed_name
        if item.source_rename_error:
            payload["source_rename_error"] = item.source_rename_error
        return payload

    @staticmethod
    def _manifest_path(item: _UmiExportItem) -> Path:
        if item.export_format == "lerobot":
            return item.output_path / "meta" / "manifest.json"
        return item.output_path.with_name(f"{item.dataset_name}.manifest.json")

    def _bag_path(self, bag_name: str) -> Path:
        bag_name = self._validate_name(bag_name, "bag name")
        if bag_name.startswith(_FAILED_BAG_PREFIX):
            raise ValueError("Failed rosbags are hidden from dataset export.")
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
