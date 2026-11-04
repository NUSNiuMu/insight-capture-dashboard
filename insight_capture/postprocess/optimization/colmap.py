"""Background trajectory optimization process manager."""

import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional

class OptimizationManager:
    """Runs the looper-vio-colmap pipeline as a background subprocess."""

    STEP_NAMES = [
        "Extracting VIO",
        "Extracting images",
        "Running COLMAP",
        "Aligning trajectories (Sim3)",
    ]
    _STEP_MARKERS = [
        "STEP 1: Extracting VIO",
        "STEP 2: Extracting images",
        "STEP 3: Running COLMAP",
        "STEP 4: Aligning trajectories",
    ]
    _MAX_LOG = 60

    # Regexes for fine-grained COLMAP sub-progress
    _RE_FEATURE = re.compile(r'Processed file \[(\d+)/(\d+)\]')
    _RE_MATCH   = re.compile(r'Matching image \[(\d+)/(\d+)\]')
    # COLMAP phases map to extraction, matching, mapper, and post-processing.
    _COLMAP_FEATURE_END  = 0.45
    _COLMAP_MATCH_START  = 0.45
    _COLMAP_MATCH_END    = 0.70
    _COLMAP_MAPPER_START = 0.70
    _COLMAP_MAPPER_END   = 0.95

    def __init__(
        self,
        project_root: Path,
        tools_dir: Path,
        on_finished: Optional[Callable[[bool], None]] = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.tools_dir = tools_dir.resolve()
        self._on_finished = on_finished
        self._lock = threading.Lock()
        self._process: Optional[subprocess.Popen] = None
        self._state: str = "idle"
        self._step: int = 0
        self._sub_progress: float = 0.0   # 0.0–1.0 within current step
        self._colmap_phase: str = ""       # "feature" | "matching" | "mapper" | "post"
        self._run_name: str = ""
        self._log: List[str] = []
        self._result: Dict = {}

    def status(self) -> Dict:
        with self._lock:
            result = dict(self._result)
            log_url = result.get("colmap_log")
            if log_url and not (self.project_root / "runs" / log_url.removeprefix("/optimization-runs/")).is_file():
                result.pop("colmap_log")
            return {
                "state": self._state,
                "step": self._step,
                "step_name": self.STEP_NAMES[self._step - 1] if 1 <= self._step <= 4 else "",
                "sub_progress": round(self._sub_progress, 4),
                "run_name": self._run_name,
                "log_tail": list(self._log[-30:]),
                "result": result,
            }

    def start(
        self,
        bag_path: Path,
        run_name: str,
        vio_topic: str,
        image_topic_str: str,
        info_topic: str,
        output_hz: float = 5.0,
    ) -> None:
        with self._lock:
            if self._process is not None:
                raise RuntimeError("Optimization already running")
            bag_path = bag_path.resolve()
            if not bag_path.exists():
                raise ValueError(f"Bag not found: {bag_path}")
            if Path(run_name).name != run_name or run_name in {".", ".."}:
                raise ValueError("Run name must be a directory name.")
            hz_label = f"{output_hz:g}"
            self._result = {
                "colmap_log": f"/optimization-runs/{run_name}/colmap/images_{hz_label}hz/colmap.log",
            }
            cmd = [
                sys.executable, "-u",  # force unbuffered stdout so step markers arrive immediately
                "-m", "insight_capture.postprocess.optimization.pipeline",
                "--bag", str(bag_path),
                "--name", run_name,
                "--vio-topic", vio_topic,
                "--image-topic", image_topic_str,
                "--info-topic", info_topic,
                "--tools-dir", str(self.tools_dir),
                "--output-hz", str(output_hz),
            ]
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=str(self.project_root),
                env=os.environ.copy(),
                start_new_session=True,
            )
            self._process = process
            self._state = "running"
            self._step = 0
            self._sub_progress = 0.0
            self._colmap_phase = ""
            self._run_name = run_name
            self._log = []
        threading.Thread(
            target=self._monitor, args=(process,), daemon=True, name="optimization_monitor"
        ).start()

    def stop(self) -> None:
        with self._lock:
            process = self._process
            if process is None:
                return
            self._state = "stopping"
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
            process.wait()

    def _monitor(self, process: subprocess.Popen) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip()
            with self._lock:
                self._log.append(line)
                if len(self._log) > self._MAX_LOG:
                    self._log = self._log[-self._MAX_LOG:]
                for i, marker in enumerate(self._STEP_MARKERS):
                    if marker in line:
                        self._step = i + 1
                        self._sub_progress = 0.0
                        self._colmap_phase = ""
                        break
                self._update_sub_progress(line)
        return_code = process.wait()
        success = return_code == 0
        with self._lock:
            stopped = self._state == "stopping"
            if self._process is process:
                self._process = None
                self._state = "stopped" if stopped else "done" if success else "error"
                if success and not stopped:
                    self._step = 4
                    self._sub_progress = 1.0
        if self._on_finished and not stopped:
            try:
                self._on_finished(success)
            except Exception:
                pass

    def _update_sub_progress(self, line: str) -> None:
        """Parse COLMAP log lines to update fine-grained sub_progress within step 3."""
        if self._step != 3:
            return

        # Detect COLMAP sub-phase transitions
        if line.startswith("$ ") and " feature_extractor " in line:
            self._colmap_phase = "feature"
            self._sub_progress = 0.0
            return
        if line.startswith("$ ") and " sequential_matcher " in line:
            self._colmap_phase = "matching"
            self._sub_progress = self._COLMAP_MATCH_START
            return
        if line.startswith("$ ") and " mapper " in line:
            self._colmap_phase = "mapper"
            self._sub_progress = self._COLMAP_MAPPER_START
            return
        if (line.startswith("$ ") and " model_converter " in line) or "COLMAP 重建完成" in line:
            self._colmap_phase = "post"
            self._sub_progress = self._COLMAP_MAPPER_END
            return

        if self._colmap_phase == "feature":
            m = self._RE_FEATURE.search(line)
            if m:
                n, total = int(m.group(1)), int(m.group(2))
                if total > 0:
                    self._sub_progress = (n / total) * self._COLMAP_FEATURE_END

        elif self._colmap_phase == "matching":
            m = self._RE_MATCH.search(line)
            if m:
                n, total = int(m.group(1)), int(m.group(2))
                if total > 0:
                    span = self._COLMAP_MATCH_END - self._COLMAP_MATCH_START
                    self._sub_progress = self._COLMAP_MATCH_START + (n / total) * span

        elif self._colmap_phase == "mapper":
            # Mapper progress is hard to quantify; nudge forward on every output line
            # so the bar visually moves, capped just below the mapper end boundary.
            nudge = 0.0005
            ceiling = self._COLMAP_MAPPER_END - nudge
            if self._sub_progress < ceiling:
                self._sub_progress = min(ceiling, self._sub_progress + nudge)
