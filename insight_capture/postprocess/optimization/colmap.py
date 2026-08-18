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
        "Extracting color images",
        "Running COLMAP",
        "Aligning trajectories (Sim3)",
    ]
    _STEP_MARKERS = [
        "1/3 提取 VIO",
        "2/3 提取 color 图片",
        "3/3 运行 COLMAP CLI",
        "4/5 Sim3 对齐 COLMAP 轨迹",
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
        pipeline_script: Path,
        on_finished: Optional[Callable[[bool], None]] = None,
        bag_resolver: Optional[Callable[[str], Path]] = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.pipeline_script = pipeline_script.resolve()
        self._on_finished = on_finished
        self._bag_resolver = bag_resolver
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
            return {
                "state": self._state,
                "step": self._step,
                "step_name": self.STEP_NAMES[self._step - 1] if 1 <= self._step <= 4 else "",
                "sub_progress": round(self._sub_progress, 4),
                "run_name": self._run_name,
                "log_tail": list(self._log[-30:]),
                "result": dict(self._result),
            }

    @staticmethod
    def _stream_name(image_topic: str) -> str:
        known = ("infra1", "infra2", "color", "depth", "fisheye")
        for part in reversed(image_topic.rstrip("/").split("/")):
            if part in known:
                return part
        return "image"

    def start(
        self,
        bag_name: str,
        run_name: str,
        vio_topic: str,
        image_topic_str: str,
        output_hz: float = 5.0,
        camera_params: str = "",
    ) -> None:
        with self._lock:
            if self._state == "running":
                raise RuntimeError("Optimization already running")
            bag_path = (
                self._bag_resolver(bag_name).resolve()
                if self._bag_resolver is not None
                else self.project_root / "rosbags" / bag_name
            )
            if not bag_path.exists():
                raise ValueError(f"Bag not found: {bag_name}")
            hz_label = str(int(output_hz)) if output_hz == int(output_hz) else str(output_hz).replace(".", "p")
            stream = self._stream_name(image_topic_str)
            self._result = {
                "colmap_log": f"/optimization-runs/{run_name}/colmap/{stream}_{hz_label}hz/colmap.log",
            }
            cmd = [
                sys.executable, "-u",  # force unbuffered stdout so step markers arrive immediately
                str(self.pipeline_script),
                "--bag", str(bag_path),
                "--name", run_name,
                "--vio-topic", vio_topic,
                "--image-topic", image_topic_str,
                "--colmap-runner", "local",
                "--output-hz", str(output_hz),
                "--make-plots", "false",
                "--overwrite", "true",
                *(["--camera-params", camera_params] if camera_params else []),
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
                self._step = 0
                self._sub_progress = 0.0
                self._colmap_phase = ""

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
            if self._process is process:
                self._process = None
                self._state = "done" if success else "error"
                if success:
                    self._step = 4
                    self._sub_progress = 1.0
        if self._on_finished:
            try:
                self._on_finished(success)
            except Exception:
                pass

    def _update_sub_progress(self, line: str) -> None:
        """Parse COLMAP log lines to update fine-grained sub_progress within step 3."""
        if self._step != 3:
            return

        # Detect COLMAP sub-phase transitions
        if "$ colmap feature_extractor" in line or "$ nice" in line and "feature_extractor" in line:
            self._colmap_phase = "feature"
            self._sub_progress = 0.0
            return
        if "$ colmap sequential_matcher" in line or "$ nice" in line and "sequential_matcher" in line:
            self._colmap_phase = "matching"
            self._sub_progress = self._COLMAP_MATCH_START
            return
        if "$ colmap mapper" in line or "$ nice" in line and "colmap mapper" in line:
            self._colmap_phase = "mapper"
            self._sub_progress = self._COLMAP_MAPPER_START
            return
        if "$ colmap model_converter" in line or "COLMAP 重建完成" in line or "完成 COLMAP CLI" in line:
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
