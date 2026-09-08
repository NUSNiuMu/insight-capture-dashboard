"""Bounded input queue and one SQLite recording writer."""

import json
import queue
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import cv2
import numpy as np

from .pose import DirectPose


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    )
    temporary.replace(path)


class Capture:
    def __init__(self, config_path, output):
        self.config = json.loads(Path(config_path).read_text())
        names = ["head"] + [x["name"] for x in self.config.get("rgb", [])]
        if len(names) != len(set(names)) or any(
            not re.fullmatch(r"[A-Za-z0-9_]+", name) for name in names
        ):
            raise ValueError("相机名称须唯一，且仅含字母、数字和下划线")
        if (
            set(self.config["arms"]) != {"left", "right"}
            or len(set(self.config["arms"].values())) != 2
        ):
            raise ValueError("左右手须分别绑定两个不同的 cube")
        if (
            not 1 <= self.config.get("fps", 20) <= 120
            or not 0 < self.config.get("sync_tolerance_ms", 40) <= 100
        ):
            raise ValueError("fps 或同步容差超出范围")
        self.root = Path(output).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.pose = DirectPose(config_path, self.config)
        self.lock = threading.RLock()
        self.queue = queue.Queue(maxsize=128)
        self.stop_event = threading.Event()
        self.stop_lock = threading.Lock()
        self.draining = False
        self.active = None
        self.db = None
        self.error = None
        self.drops = {}
        self.latest = {}
        self.preview = {}
        self.observation = {}
        self.intrinsic = None
        self.imu_from_rgb = None
        self.calibration = {}
        self.started = 0.0
        self.session_generation = 0
        self.last_commit = 0.0
        self.reference_pending = False
        self.worker = threading.Thread(target=self._work, daemon=True)
        self.worker.start()

    def submit(self, kind, name, stamp_ns, value, received=None):
        now = time.monotonic() if received is None else received
        if self.draining or self.stop_event.is_set():
            return
        self.latest[("image" if kind == "raw_image" else kind) + "/" + name] = now
        try:
            session = self.active if now >= self.started else None
            self.queue.put_nowait((session, kind, name, now, int(stamp_ns), value))
        except queue.Full:
            key = kind + "/" + name
            self.drops[key] = self.drops.get(key, 0) + 1

    def start(self):
        with self.lock:
            if self.active or self.draining:
                raise ValueError("已经在录制或正在停止")
            if (
                time.monotonic() - self.latest.get("image/head", 0) > 2
                or self.intrinsic is None
                or self.imu_from_rgb is None
            ):
                raise ValueError("等待 Insight9 RGB、CameraInfo 和 IMU→RGB 外参")
            if (
                self.pose.last_vio is None
                or time.monotonic() - self.latest.get("vio/head", 0) > 2
            ):
                raise ValueError("等待 Insight9 VIO")
            name = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
            directory = self.root / name
            directory.mkdir()
            self.started = time.monotonic()
            self.session_generation = self.pose.generation
            self.reference_pending = True
            self.error = None
            self.drops = {}
            try:
                self.db = sqlite3.connect(
                    directory / "capture.sqlite3", check_same_thread=False
                )
                self.db.execute("PRAGMA journal_mode=WAL")
                self.db.execute(
                    "CREATE TABLE samples(kind TEXT, name TEXT, t REAL, stamp_ns INTEGER, payload BLOB)"
                )
                self.db.execute("CREATE INDEX sample_time ON samples(kind,name,t)")
                write_json(
                    directory / "session.json",
                    {
                        "config": self.config,
                        "started_unix_ns": time.time_ns(),
                        "clock": "host_monotonic_receive",
                        "status": "recording",
                        "calibration": self.calibration,
                    },
                )
                write_json(directory / "segments.json", [])
            except Exception:
                if self.db is not None:
                    self.db.close()
                    self.db = None
                raise
            self.active = name
            return self.active

    def stop(self):
        with self.stop_lock:
            with self.lock:
                if not self.active:
                    return None
                self.draining = True
                stopped = time.monotonic()
            self.queue.join()
            with self.lock:
                name = self.active
                self.active = None
                try:
                    self.db.commit()
                    path = self.root / name / "session.json"
                    meta = json.loads(path.read_text())
                    meta.update(
                        duration_s=stopped - self.started,
                        status="stopped",
                        error=self.error,
                        queue_drops=self.drops,
                    )
                    write_json(path, meta)
                finally:
                    self.db.close()
                    self.db = None
                    self.draining = False
                return name

    def _save(self, session, kind, name, seconds, stamp, payload):
        if self.active is not None and self.db is not None and session == self.active:
            self.db.execute(
                "INSERT INTO samples VALUES(?,?,?,?,?)",
                (kind, name, seconds - self.started, stamp, payload),
            )

    def _work(self):
        while not self.stop_event.is_set():
            try:
                session, kind, name, seconds, stamp, value = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if kind == "raw_image":
                    from insight_capture.postprocess.gripper.extraction import (
                        decode_color_image,
                    )

                    bgr = decode_color_image(value, "sensor_msgs/msg/Image")
                    if bgr is None:
                        raise ValueError("无法解码 ROS Image")
                    ok, jpeg = cv2.imencode(".jpg", bgr)
                    if not ok:
                        raise ValueError("JPEG 编码失败")
                    kind, value = "image", jpeg.tobytes()
                # Future VIO samples arrive independently of this processing thread.
                head = None
                if kind == "image" and name == "head":
                    deadline = time.monotonic() + 0.05
                    head = self.pose.buffer.lookup(stamp)
                    while head is None and time.monotonic() < deadline:
                        time.sleep(0.002)
                        head = self.pose.buffer.lookup(stamp)
                with self.lock:
                    payload = (
                        value
                        if kind == "image"
                        else json.dumps(value, allow_nan=False).encode()
                    )
                    self._save(session, kind, name, seconds, stamp, payload)
                    if kind == "image":
                        self.preview[name] = value
                    if kind == "image" and name == "head":
                        if self.reference_pending and session == self.active:
                            self.pose.reset_reference()
                        if (
                            head is None
                            or self.intrinsic is None
                            or self.imu_from_rgb is None
                        ):
                            self.observation = {
                                x: {
                                    "valid": False,
                                    "reason": "missing_head_pose_or_calibration",
                                }
                                for x in self.pose.config.targets
                            }
                        elif (
                            self.active
                            and self.pose.generation != self.session_generation
                        ):
                            self.error = "Insight9 VIO 重置或跳变，请停止后重新录制"
                            self.observation = {
                                x: {"valid": False, "reason": "head_vio_reset"}
                                for x in self.pose.config.targets
                            }
                        else:
                            gray = cv2.imdecode(
                                np.frombuffer(value, np.uint8), cv2.IMREAD_GRAYSCALE
                            )
                            if gray is None:
                                raise ValueError("无法解码头部图像")
                            generation = self.pose.generation
                            self.observation = self.pose.observe(
                                gray, self.intrinsic, head, self.imu_from_rgb, seconds
                            )
                            if generation != self.pose.generation:
                                self.observation = {
                                    x: {"valid": False, "reason": "head_vio_reset"}
                                    for x in self.pose.config.targets
                                }
                            if self.reference_pending and session == self.active:
                                path = self.root / self.active / "session.json"
                                meta = json.loads(path.read_text())
                                meta.update(
                                    reference_stamp_ns=stamp,
                                    reference_from_vio_world=self.pose.reference.tolist(),
                                    imu_from_rgb=self.imu_from_rgb.tolist(),
                                )
                                write_json(path, meta)
                                self.reference_pending = False
                        self._save(
                            session,
                            "pose",
                            "arms",
                            seconds,
                            stamp,
                            json.dumps(self.observation).encode(),
                        )
                    if self.db and time.monotonic() - self.last_commit > 1:
                        self.db.commit()
                        self.last_commit = time.monotonic()
            except Exception as exc:
                self.error = str(exc)
            finally:
                self.queue.task_done()

    def status(self):
        with self.lock:
            now = time.monotonic()
            return {
                "recording": self.active,
                "elapsed_s": now - self.started if self.active else 0,
                "error": self.error,
                "queue_drops": dict(self.drops),
                "sources_age_s": {k: round(now - v, 2) for k, v in self.latest.items()},
                "calibrated": self.intrinsic is not None
                and self.imu_from_rgb is not None,
                "arms": self.observation
                if now - self.latest.get("image/head", 0) < 1
                else {},
                "streams": ["head"] + [x["name"] for x in self.config.get("rgb", [])],
            }

    def close(self):
        self.stop()
        self.stop_event.set()
        self.worker.join(2)
