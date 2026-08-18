"""Background trajectory scoring service."""

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class _ScoringJob:
    bag_name: str
    bag_path: str
    topic: str          # empty = auto-discover all; non-empty = score only this topic
    status: str         # "running" | "done" | "error"
    result: Optional[Dict] = None
    error: Optional[str] = None
    started_at: float = 0.0
    finished_at: float = 0.0
    current_topic: str = ""  # which topic is being scored right now


class ScoringManager:
    _TRAJ_SCORE = Path(__file__).resolve().parents[2] / "scripts" / "traj_score.py"

    def __init__(self, rosbag_root: Path, results_root: Path) -> None:
        self.rosbag_root = rosbag_root
        self.results_root = results_root
        self._lock = threading.Lock()
        self._current_job: Optional[_ScoringJob] = None

    @property
    def status(self) -> Dict:
        with self._lock:
            if self._current_job is None:
                return {"status": "idle"}
            job = self._current_job
            payload: Dict = {
                "status": job.status,
                "bag_name": job.bag_name,
                "topic": job.current_topic or job.topic,
                "started_at": job.started_at,
            }
            if job.result is not None:
                payload["result"] = job.result
            if job.error is not None:
                payload["error"] = job.error
            if job.finished_at:
                payload["finished_at"] = job.finished_at
            return payload

    def run(self, bag_name: str, topic: str = "") -> bool:
        """Start a new scoring job. Returns False if a job is already running."""
        with self._lock:
            if self._current_job and self._current_job.status == "running":
                return False
            bag_path = str((self.rosbag_root / bag_name).resolve())
            job = _ScoringJob(
                bag_name=bag_name,
                bag_path=bag_path,
                topic=topic,
                status="running",
                started_at=time.monotonic(),
            )
            self._current_job = job
        threading.Thread(target=self._worker, args=(job,), daemon=True, name="traj_score").start()
        return True

    def _worker(self, job: _ScoringJob) -> None:
        try:
            if job.topic:
                topics = [job.topic]
            else:
                topics = self._find_cov_topics(job.bag_path)
            if not topics:
                raise RuntimeError(
                    "No PoseWithCovarianceStamped topic found in bag. Specify the topic explicitly."
                )

            scores_dir = self.results_root / "scores"
            scores_dir.mkdir(parents=True, exist_ok=True)

            cameras = []
            for topic in topics:
                with self._lock:
                    job.current_topic = topic

                safe_name = topic.replace("/", "_").strip("_")
                output_json = scores_dir / f"{job.bag_name}__{safe_name}.json"

                cmd = [
                    "/usr/bin/python3",
                    str(self._TRAJ_SCORE),
                    job.bag_path,
                    "--topic", topic,
                    "--json", str(output_json),
                ]
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=300, env=os.environ.copy()
                )
                if proc.returncode != 0:
                    cameras.append({
                        "topic": topic,
                        "error": (proc.stderr or proc.stdout).strip(),
                    })
                    continue

                with output_json.open(encoding="utf-8") as fh:
                    data = json.load(fh)
                cameras.append(data)

            summary = {"cameras": cameras}
            summary_json = scores_dir / f"{job.bag_name}.json"
            with summary_json.open("w", encoding="utf-8") as fh:
                json.dump(summary, fh, indent=2)

            with self._lock:
                job.status = "done"
                job.current_topic = ""
                job.result = summary
                job.finished_at = time.monotonic()

        except Exception as exc:
            with self._lock:
                job.status = "error"
                job.error = str(exc)
                job.finished_at = time.monotonic()

    def _find_cov_topics(self, bag_path: str) -> List[str]:
        """Scan bag topics and return all PoseWithCovarianceStamped topics."""
        cmd = ["/usr/bin/python3", str(self._TRAJ_SCORE), bag_path, "--list-topics"]
        found = []
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30, env=os.environ.copy()
            )
            for line in proc.stdout.splitlines():
                stripped = line.strip()
                if "PoseWithCovarianceStamped" in stripped:
                    topic = stripped.split("[")[0].strip()
                    if topic.startswith("/"):
                        found.append(topic)
        except Exception:
            pass
        return found
