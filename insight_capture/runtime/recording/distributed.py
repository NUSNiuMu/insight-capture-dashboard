"""Coordinate wrist recording on Jetson and authoritative head recording on TF."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
import threading
import time
import uuid

from .clock_sync import measure_clocks
from .distributed_merge import file_hash, merge_session, write_json
from .ego_participant import EgoParticipant


TERMINAL = {"done", "error"}


class DistributedRecordingManager:
    def __init__(self, local, config: dict, state_path: Path):
        self.local, self.config, self.state_path = local, config, state_path
        self.ego = EgoParticipant(config)
        self._lock = threading.RLock()
        self._lifecycle = threading.Lock()
        self._callbacks = []
        self._finished_callbacks = []
        self._clock = {}
        self._last_clock_mono = 0.0
        self._baseline = None
        self._session = {}
        self._worker = None
        self._last_error = None
        self._bypass = False
        if state_path.exists():
            self._session = json.loads(state_path.read_text())
            samples = self._session.get("clock_samples") or []
            self._clock = deepcopy(samples[-1]) if samples else {}
            if self._session.get("phase") not in TERMINAL:
                if self._session.get("ego_url") != self.ego.base:
                    raise RuntimeError("未完成的双端录制属于另一台 Ego，请先恢复原连接")
                self._session.update(stop_requested=True, phase="stop_pending", interrupted=True)
                self._save()
        self.local.add_recording_completed_callback(self._local_completed)
        threading.Thread(target=self._monitor, daemon=True, name="distributed-recording").start()

    def __getattr__(self, name):
        return getattr(self.local, name)

    @property
    def merge_state(self):
        return self.status()["merge_state"]

    def add_recording_completed_callback(self, callback):
        self._callbacks.append(callback)

    def add_session_finished_callback(self, callback):
        self._finished_callbacks.append(callback)

    def _local_completed(self, path):
        if not self._session or str(path) != self._session.get("jetson_path"):
            for callback in self._callbacks:
                callback(path)

    def _save(self):
        with self._lock:
            write_json(self.state_path, self._session)
            path = self._session.get("manifest_path")
            if path:
                write_json(Path(path), self._session)

    def _update(self, **values):
        with self._lock:
            self._session.update(values)
            self._save()

    def _clocks(self):
        report = measure_clocks(self.config)
        with self._lock:
            self._clock = report
            self._last_clock_mono = time.monotonic()
        return report

    def distributed_preflight(self):
        if self._session and self._session.get("phase") not in TERMINAL:
            raise RuntimeError("上一条双端录制尚未保存或合并，请先等待或重试")
        remote = self.ego.preflight()
        clocks = self._clocks()
        if not clocks["ok"]:
            names = [name for name, item in clocks["devices"].items() if not item["ok"]]
            raise RuntimeError("设备校时检查失败：" + ("、".join(names) or "设备间偏差界限超过门槛"))
        return {"ok": True, "clocks": clocks, "ego_storage": {k: remote["storage"].get(k) for k in ("path", "free_bytes", "mounted", "writable")}}

    def start(self, topics=None, bag_name=None, recording_mode="capture", output_subdirectory=None, task=None):
        if recording_mode != "capture":
            if self._session and self._session.get("phase") not in TERMINAL:
                raise RuntimeError("上一条双端录制尚未结束")
            self._bypass = True
            return self.local.start(topics, bag_name, recording_mode, output_subdirectory)
        if not self._lifecycle.acquire(blocking=False):
            raise RuntimeError("双端录制正在启停，请稍候")
        try:
            if self.local.is_recording():
                raise RuntimeError("Recording is already running.")
            self._bypass = False
            self.distributed_preflight()
            remote = self.ego.preflight()
            task = task or {}
            task_name = task.get("task_name") or task.get("task") or output_subdirectory or "Ego capture"
            task_id = self.ego.task(task_name, remote["storage"]["media"]["token"])
            sid = uuid.uuid4().hex
            selected = self.local.default_topics if topics is None else list(topics)
            selected = [topic for topic in selected if not topic.startswith(self.config["ego_topic_prefix"])]
            required = [topic for topic in self.config["image_topics"] if not topic.startswith(self.config["ego_topic_prefix"])]
            if not all(topic in selected for topic in required):
                raise RuntimeError("双端录制必须包含左右腕部图像")
            # Resolve storage before assigning both participant paths to the same filesystem.
            with self.local._lock:
                self.local._refresh_recording_storage_unlocked()
            root = self.local.rosbag_root
            if output_subdirectory and not re.fullmatch(r"[0-9A-Za-z_-]+", output_subdirectory):
                raise ValueError("Recording subdirectory must be a single safe task-set name.")
            name = bag_name or ("ego_capture_" + sid)
            if not name or any(c not in "-_" and not c.isalnum() for c in name):
                raise ValueError("Invalid distributed recording name")
            target = root / output_subdirectory / name if output_subdirectory else root / name
            if target.exists():
                raise RuntimeError("录制输出已存在：" + str(target))
            with self._lock:
                self._session = {
                    "schema_version": 1, "session_id": sid, "phase": "starting",
                    "task_id": task.get("task_id") or output_subdirectory, "task_name": task_name,
                    "take_id": task.get("take_id"), "ego_url": self.ego.base,
                    "start_command": "dashboard_start_" + sid, "stop_command": "dashboard_stop_" + sid,
                    "created_at_epoch_s": time.time(), "stop_requested": False,
                    "jetson_path": str(root / "_distributed" / (sid + "_jetson")),
                    "ego_copy_path": str(root / "_distributed" / (sid + "_ego")),
                    "output_path": str(target),
                    "manifest_path": str(root / "_distributed" / (sid + ".json")),
                    "clock_samples": [deepcopy(self._clock)], "clock_violations": [],
                    "local_saved": False, "remote_saved": False,
                }
                self._last_error = None
                self._baseline = (time.time(), time.monotonic())
                self._save()
            try:
                # Extra lead-in permits DDS warm-up and a complete head keyframe before action.
                self.local.start(selected, sid + "_jetson", "capture", "_distributed")
                self._update(jetson_path=self.local.output_path, local_started_at_epoch_s=time.time(), remote_start_attempted=True)
                try:
                    self.ego.request("/api/recording/start", {"command_id": self._session["start_command"], "session_id": sid, "task_id": task_id},
                                     headers={"X-Capture-Media": remote["storage"]["media"]["token"]})
                except OSError:
                    # The server may have accepted it. Resolve the original command, never create another.
                    pass
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    record = self.ego.lookup(self._session)
                    if record and record["status"] == "recording":
                        break
                    if record and record["status"] not in {"preparing", "recording"}:
                        raise RuntimeError("Ego 起录失败：" + record["status"])
                    time.sleep(0.2)
                else:
                    raise RuntimeError("Ego 起录确认超时")
                # At least one full GOP after the recording becomes active, before displaying ready.
                time.sleep(float(self.config.get("keyframe_lead_in_sec", 1.1)))
                clocks = self._clocks()
                self._session["clock_samples"].append(clocks)
                if not clocks["ok"]:
                    raise RuntimeError("起录过程中设备时钟检查失败")
                self._update(phase="recording", remote_recording_id=record["id"], remote_path=record["path"], ready_at_epoch_s=time.time())
            except Exception as exc:
                self._update(stop_requested=True, phase="stop_pending", start_error=str(exc))
                if self.local.is_recording():
                    self.local.stop()
                self._update(local_saved=Path(self._session["jetson_path"], "metadata.yaml").is_file())
                try:
                    self.ego.cancel(self._session)
                except Exception:
                    pass
                raise
            return self.status()
        finally:
            self._lifecycle.release()

    def stop(self, timeout_sec=30.0, *, expected_recording=None):
        if self._bypass:
            return self.local.stop(timeout_sec, expected_recording=expected_recording)
        with self._lifecycle:
            if expected_recording is not None and expected_recording != (
                self._session.get("output_path"), "capture"
            ):
                raise RuntimeError("采集已变化，停止请求未执行，请刷新状态。")
            if not self._session or self._session.get("phase") in TERMINAL:
                return self.local.stop(timeout_sec, expected_recording=expected_recording) if self.local.is_recording() else self.status()
            self._update(stop_requested=True, phase="stop_pending", stopped_at_epoch_s=time.time())
            # Request both stops concurrently; local finalization never waits for network recovery.
            def stop_ego():
                try:
                    self.ego.cancel(self._session)
                except Exception as exc:
                    self._last_error = str(exc)
            remote_thread = threading.Thread(target=stop_ego, daemon=True)
            remote_thread.start()
            if self.local.is_recording():
                result = self.local.stop(timeout_sec)
                self._update(local_saved=result.get("merge_state") == "done", local_error=result.get("merge_error"))
            remote_thread.join(timeout=self.ego.timeout * 2 + 1)
            clocks = self._clocks()
            self._record_clock(clocks)
            self._save()
            return self.status()

    def _record_clock(self, clocks):
        with self._lock:
            self._session.setdefault("clock_samples", []).append(clocks)
            wall, mono = self._baseline or (time.time(), time.monotonic())
            step_ms = abs((time.time() - wall) - (time.monotonic() - mono)) * 1000
            if not clocks["ok"] or step_ms > float(self.config.get("max_clock_bound_ms", 10)):
                self._session.setdefault("clock_violations", []).append({"at_epoch_s": time.time(), "reason": "clock check failed or Jetson clock step", "wall_monotonic_delta_ms": step_ms})
            self._save()

    def _finish(self):
        for callback in self._finished_callbacks:
            try:
                callback(self.status())
            except Exception as exc:
                self._last_error = str(exc)

    def _merge(self):
        self._lifecycle.acquire()
        try:
            output = Path(self._session["output_path"])
            if output.exists():
                published = json.loads((output / "distributed_session.json").read_text())
                report = json.loads((output / "time_alignment.json").read_text())
                if published.get("session_id") != self._session["session_id"] or report.get("session_id") != self._session["session_id"] or not (output / "metadata.yaml").is_file():
                    raise RuntimeError("已有输出不属于本次双端录制，拒绝覆盖")
                # Atomic publication may have completed immediately before a service crash.
                self._update(phase="done", alignment=report, finished_at_epoch_s=time.time(), error=None)
                self._finish()
                for callback in self._callbacks:
                    try:
                        callback(output)
                    except Exception as exc:
                        self._last_error = str(exc)
                return
            self._update(phase="copying")
            ego = Path(self._session["ego_copy_path"])
            verification = self.ego.download(self._session, ego)
            self._update(ego_copy_verification=verification, phase="merging")
            wrists = Path(self._session["jetson_path"])
            hashes = [{"name": path.name, "sha256": file_hash(path), "bytes": path.stat().st_size} for path in wrists.glob("*.mcap")]
            self._update(jetson_files=hashes)
            output.parent.mkdir(parents=True, exist_ok=True)
            report = merge_session(deepcopy(self._session), self.config, ego, wrists, output)
            self._update(phase="done", alignment=report, finished_at_epoch_s=time.time(), error=None)
            write_json(output / "distributed_session.json", deepcopy(self._session))
            self._last_error = None
            self._finish()
            for callback in self._callbacks:
                try:
                    callback(output)
                except Exception as exc:
                    self._last_error = str(exc)
        except Exception as exc:
            self._update(phase="error", error=str(exc))
            self._last_error = str(exc)
            self._finish()
        finally:
            self._lifecycle.release()

    def retry_merge(self):
        with self._lifecycle:
            if not self._session or self._session.get("phase") != "error" or not self._session.get("remote_saved") or not self._session.get("local_saved"):
                raise RuntimeError("没有可重试合并的已保存双端录制")
            self._update(phase="saved", error=None)
        return self.status()

    def _monitor(self):
        while True:
            time.sleep(1)
            if not self._session or self._session.get("phase") in TERMINAL or not self._lifecycle.acquire(blocking=False):
                continue
            try:
                phase = self._session["phase"]
                if phase in {"copying", "merging"}:
                    continue
                if phase == "recording":
                    if time.monotonic() - self._last_clock_mono >= float(self.config.get("clock_sample_interval_sec", 5)):
                        self._record_clock(self._clocks())
                    record = self.ego.lookup(self._session)
                    if not record or record["status"] != "recording" or not self.local.is_recording():
                        self._update(stop_requested=True, phase="stop_pending", start_error="某一端提前结束录制", stopped_at_epoch_s=time.time())
                    continue
                if self._session.get("stop_requested"):
                    if self.local.is_recording():
                        result = self.local.stop()
                        self._update(local_saved=result.get("merge_state") == "done", local_error=result.get("merge_error"))
                    record = self.ego.cancel(self._session)
                    if record and record["status"] == "complete":
                        self._update(remote_saved=True, remote_recording_id=record["id"], remote_path=record["path"])
                    elif record and record["status"] in {"error", "interrupted"}:
                        self._update(phase="error", error="Ego 录制保存失败：" + record["status"])
                        self._finish()
                        continue
                    elif record is None:
                        # Cancel acknowledgement makes a delayed original start impossible.
                        self._update(phase="error", error=self._session.get("start_error") or "Ego 未起录，双端原始文件保留")
                        self._finish()
                        continue
                    else:
                        continue
                    local_saved = Path(self._session["jetson_path"], "metadata.yaml").is_file()
                    staging = Path(self._session["jetson_path"]).parents[1] / "_staging" / Path(self._session["jetson_path"]).name
                    if not local_saved and self._session.get("interrupted") and staging.exists():
                        self._last_error = "等待 Jetson 中断录制恢复，Ego 原件已保存"
                        continue
                    self._update(local_saved=local_saved)
                    if not local_saved or self._session.get("local_error") or self._session.get("start_error"):
                        self._update(phase="error", error=self._session.get("local_error") or self._session.get("start_error") or "Jetson 原始文件尚未保存，请检查中断恢复")
                        self._finish()
                        continue
                    self._update(phase="saved")
                if self._session.get("phase") == "saved":
                    self._worker = threading.Thread(target=self._merge, daemon=True, name="distributed-merge")
                    self._update(phase="copying")
                    self._worker.start()
            except Exception as exc:
                self._last_error = str(exc)
            finally:
                self._lifecycle.release()

    def is_recording(self):
        if self._bypass:
            return self.local.is_recording()
        return self.local.is_recording() or self._session.get("phase") in {"starting", "recording", "stop_pending"}

    def status(self):
        result = self.local.status()
        if self._bypass:
            return result
        with self._lock:
            session = deepcopy(self._session)
            phase = session.get("phase", "idle")
            active = phase in {"starting", "recording", "stop_pending"}
            if session:
                result.update(recording=active, output_path=session["output_path"],
                              merge_state="done" if phase == "done" else "error" if phase == "error" else "merging" if phase in {"saved", "copying", "merging"} else "finalizing" if phase == "stop_pending" else "idle",
                              merge_error=session.get("error"))
                if phase == "done":
                    result["merge_timings"] = {
                        "method": "distributed_copy_merge",
                        "total_sec": round(session["finished_at_epoch_s"] - session["stopped_at_epoch_s"], 3),
                    }
            result["distributed"] = {
                "enabled": True, "phase": phase, "session_id": session.get("session_id"),
                "ego_path": session.get("remote_path"), "jetson_path": session.get("jetson_path"),
                "local_saved": session.get("local_saved", False), "remote_saved": session.get("remote_saved", False),
                "clock": deepcopy(self._clock), "clock_violations": session.get("clock_violations", []),
                "quality_issues": session.get("alignment", {}).get("video_header_quality", {}).get("issues", []),
                "error": self._last_error or session.get("error"),
                "retry_merge": phase == "error" and session.get("local_saved", False) and session.get("remote_saved", False),
            }
        return result
