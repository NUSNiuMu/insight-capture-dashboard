"""Local and SSH rosbag synchronization."""

import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, Optional


class RecordingSync:
    def __init__(self, owner) -> None:
        self.owner = owner

    def _build_sync_target_path(self, source_path: Path) -> Path:
        assert self.owner.host_sync_dir is not None
        target_path = self.owner.host_sync_dir / source_path.name
        if not target_path.exists():
            return target_path
        suffix = time.strftime("%Y%m%d_%H%M%S")
        return self.owner.host_sync_dir / f"{source_path.name}_sync_{suffix}"

    def _remote_sync_target(self, source_path: Path) -> str:
        return f"{self.owner.host_sync_ssh_target.rstrip('/')}/{source_path.name}"

    def _sync_recording_to_remote_host(self, source_path: Path) -> Dict[str, object]:
        target_path = self.owner._remote_sync_target(source_path)
        parent_target = self.owner.host_sync_ssh_target.rstrip("/")
        mkdir_cmd = [
            "ssh",
            self.owner.host_sync_ssh_target.split(":", 1)[0],
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
        with self.owner._lock:
            if source_raw is None:
                source_raw = self.owner.output_path
            if not source_raw:
                raise RuntimeError("No recorded rosbag is available to sync.")
            source_path = Path(source_raw).resolve()
            self.owner.last_sync_status = {
                "state": "syncing",
                "message": "Syncing rosbag to host...",
                "source_path": str(source_path),
                "target_path": None,
                "finished_at": None,
            }

        if self.owner.host_sync_dir is None and not self.owner.host_sync_ssh_target:
            status = {
                "state": "disabled",
                "message": "Host sync directory is not configured.",
                "source_path": str(source_path),
                "target_path": None,
                "finished_at": time.time(),
            }
            with self.owner._lock:
                self.owner.last_sync_status = status
            return status
        if not source_path.exists():
            status = {
                "state": "error",
                "message": f"Recorded rosbag path does not exist: {source_path}",
                "source_path": str(source_path),
                "target_path": None,
                "finished_at": time.time(),
            }
            with self.owner._lock:
                self.owner.last_sync_status = status
            return status

        if self.owner.host_sync_ssh_target:
            status = self.owner._sync_recording_to_remote_host(source_path)
        else:
            try:
                assert self.owner.host_sync_dir is not None
                self.owner.host_sync_dir.mkdir(parents=True, exist_ok=True)
                target_path = self.owner._build_sync_target_path(source_path)
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

        with self.owner._lock:
            self.owner.last_sync_status = status
        return status
