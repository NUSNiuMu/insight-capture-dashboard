"""Which TCP trajectory a session uses (live or offline recompute), and its smoothing.

Review, auto-segmentation and export all read poses through ``align()``,
which applies the per-session choice stored in ``pose.json``, so what the
review page draws is exactly what gets exported.

* live: the pose rows computed while recording (``capture.sqlite3``).
* offline: every recorded head frame re-detected with full-frame AprilTag
  corner refinement (too slow for the live loop), written to
  ``pose_offline.sqlite3``; the raw recording is never modified.

Smoothing runs on whichever source is selected: a Hampel filter removes
isolated single-face pose flips, then a zero-phase (forward-backward)
Butterworth low-pass removes high-frequency jitter without adding lag.
"""

import json
import multiprocessing
import os
import sqlite3
import threading
import time
from bisect import bisect_left
from concurrent.futures import ProcessPoolExecutor

import cv2
import numpy as np
from scipy.signal import butter, filtfilt
from scipy.spatial.transform import Rotation

from insight_capture.runtime.mapping.geometry import PoseSample, interpolate_pose
from .capture import write_json
from .pose import DirectPose, marker_detector

SETTINGS = "pose.json"
OFFLINE_DB = "pose_offline.sqlite3"
OFFLINE_REFINEMENT = "apriltag"
DEFAULT_FILTER_HZ = 6.0
# Same bracket limit as the live PoseBuffer: never interpolate VIO across a gap.
MAX_VIO_GAP_NS = 50_000_000
CHUNK = 48

settings_lock = threading.Lock()


def _read(path):
    file = path / SETTINGS
    return json.loads(file.read_text()) if file.is_file() else {}


def pose_settings(path, meta=None):
    meta = meta or json.loads((path / "session.json").read_text())
    stored = _read(path)
    return {
        "source": stored.get("source", "live"),
        "filter_hz": float(
            stored.get("filter_hz", meta["config"].get("pose_filter_hz", DEFAULT_FILTER_HZ))
        ),
        "offline": stored.get("offline"),
    }


def update_pose_settings(path, values):
    meta = json.loads((path / "session.json").read_text())
    nyquist = int(meta["config"].get("fps", 20)) / 2
    with settings_lock:
        stored = _read(path)
        if "source" in values:
            if values["source"] not in ("live", "offline"):
                raise ValueError("轨迹来源只能是 live 或 offline")
            if values["source"] == "offline" and not (path / OFFLINE_DB).is_file():
                raise ValueError("这条录制还没有离线重算结果，请先点击“离线重算”")
            stored["source"] = values["source"]
        if "filter_hz" in values:
            hz = float(values["filter_hz"])
            if not (hz == 0 or 0.5 <= hz < nyquist):
                raise ValueError(
                    f"低通截止频率须为 0（关闭）或 0.5–{nyquist:g} Hz 之间（采样 {nyquist * 2:g} Hz）"
                )
            stored["filter_hz"] = hz
        write_json(path / SETTINGS, stored)
    return pose_settings(path, meta)


def _hampel(values, half=3, sigmas=3.0, floor=0.0):
    """Replace samples further than ``sigmas`` robust SDs from their window median."""
    padded = np.pad(values, ((half, half), (0, 0)), mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, 2 * half + 1, axis=0)
    median = np.median(windows, axis=-1)
    mad = 1.4826 * np.median(np.abs(windows - median[..., None]), axis=-1)
    outlier = np.abs(values - median) > sigmas * np.maximum(mad, floor)
    return np.where(outlier, median, values)


def smooth_arm(block, mask, fps, cutoff_hz):
    """Smooth one arm's [xyz, rotation rows 0-1] state columns in place.

    Each contiguous valid run is filtered on its own so nothing is
    interpolated across a gap. Rotations are filtered as sign-continuous
    quaternions and re-normalised, which is accurate for the small
    frame-to-frame changes involved."""
    if not cutoff_hz:
        return
    b, a = butter(2, cutoff_hz / (fps / 2)) if cutoff_hz < fps / 2 else (None, None)
    edges = np.diff(np.r_[False, mask, False].astype(int))
    for start, end in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)):
        if end - start < 3:
            continue
        run = block[start:end].astype(np.float64)
        rows = run[:, 3:9].reshape(-1, 2, 3)
        matrices = np.concatenate([rows, np.cross(rows[:, 0], rows[:, 1])[:, None]], 1)
        quaternion = Rotation.from_matrix(matrices).as_quat()
        flips = np.r_[False, np.sum(quaternion[1:] * quaternion[:-1], 1) < 0]
        quaternion[np.cumsum(flips) % 2 == 1] *= -1
        position = _hampel(run[:, :3], floor=0.001)
        quaternion = _hampel(quaternion, floor=0.002)
        # filtfilt's default padding needs more than 3 * max(len(a), len(b)) samples.
        if b is not None and end - start > 9:
            position = filtfilt(b, a, position, axis=0)
            quaternion = filtfilt(b, a, quaternion, axis=0)
        rotation = Rotation.from_quat(quaternion).as_matrix()
        block[start:end, :3] = position
        block[start:end, 3:9] = rotation[:, :2].reshape(-1, 6)


def _worker_init():
    # Parallelism comes from the process pool; keep each worker single
    # threaded and below the capture/web process in scheduling priority.
    cv2.setNumThreads(1)
    try:
        os.nice(10)
    except OSError:
        pass


def _detect_chunk(job):
    database, rowids, size, dictionary, refinement = job
    detector = marker_detector(
        cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, dictionary)), refinement
    )
    result = []
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
        for rowid in rowids:
            (payload,) = db.execute(
                "SELECT payload FROM samples WHERE rowid=?", (rowid,)
            ).fetchone()
            gray = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_GRAYSCALE)
            if gray is not None and gray.shape == size and size[0] != size[1]:
                # Recorded copy is rotated 90° clockwise for display; the
                # intrinsics belong to the camera's own orientation.
                gray = cv2.rotate(gray, cv2.ROTATE_90_COUNTERCLOCKWISE)
            if gray is None or gray.shape[::-1] != size:
                result.append(None)
                continue
            corners, ids, _ = detector.detectMarkers(gray)
            result.append(
                {}
                if ids is None
                else {
                    int(i): np.asarray(c, np.float64).reshape(4, 2)
                    for c, i in zip(corners, ids.reshape(-1))
                }
            )
    return result


def _vio_lookup(db):
    samples = sorted(
        (
            PoseSample(
                int(stamp),
                np.array(value["position"], float),
                np.array(value["quaternion_xyzw"], float),
            )
            for stamp, value in (
                (row[0], json.loads(row[1]))
                for row in db.execute(
                    "SELECT stamp_ns,payload FROM samples WHERE kind='vio' AND stamp_ns IS NOT NULL"
                )
            )
        ),
        key=lambda x: x.stamp_ns,
    )
    stamps = [x.stamp_ns for x in samples]

    def lookup(stamp):
        index = bisect_left(stamps, stamp)
        if index < len(stamps) and stamps[index] == stamp:
            return samples[index]
        if index == 0 or index == len(stamps):
            return None
        first, second = samples[index - 1], samples[index]
        if stamp - first.stamp_ns > MAX_VIO_GAP_NS or second.stamp_ns - stamp > MAX_VIO_GAP_NS:
            return None
        return interpolate_pose(first, second, stamp)

    return lookup


def recompute(path, progress=lambda done, total: None, workers=None):
    """Re-detect every head frame and write ``pose_offline.sqlite3``."""
    started = time.monotonic()
    meta = json.loads((path / "session.json").read_text())
    if meta["status"] != "stopped":
        raise ValueError("请先停止录制，再离线重算")
    config = meta["config"]
    calibration = meta.get("calibration")
    if not calibration or meta.get("imu_from_rgb") is None:
        raise ValueError("录制缺少相机内参或 IMU–RGB 外参，无法离线重算")
    intrinsic = np.array(calibration["intrinsic"], float)
    size = (int(calibration["width"]), int(calibration["height"]))
    imu_from_rgb = np.array(meta["imu_from_rgb"], float)
    vio_enabled = config.get("vio_enabled", True)
    database = path / "capture.sqlite3"
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
        frames = db.execute(
            "SELECT rowid,t,stamp_ns FROM samples WHERE kind='image' AND name='head' ORDER BY t"
        ).fetchall()
        lookup = _vio_lookup(db) if vio_enabled else None
    if not frames:
        raise ValueError("录制中没有 Insight9 画面，无法离线重算")
    dictionary = config["cube_marker_relative_localization"].get("dictionary", "DICT_4X4_50")
    chunks = [
        (str(database), [f[0] for f in frames[i : i + CHUNK]], size, dictionary, OFFLINE_REFINEMENT)
        for i in range(0, len(frames), CHUNK)
    ]
    workers = workers or max(1, min((os.cpu_count() or 2) - 1, 12))
    detections = []
    progress(0, len(frames))
    # spawn: the service process runs ROS and aiohttp threads, which a fork
    # would copy mid-flight.
    with ProcessPoolExecutor(
        workers, mp_context=multiprocessing.get_context("spawn"), initializer=_worker_init
    ) as pool:
        for result in pool.map(_detect_chunk, chunks):
            detections.extend(result)
            progress(len(detections), len(frames))

    pose = DirectPose(config, OFFLINE_REFINEMENT)
    pose.reference = np.array(meta.get("reference_from_vio_world", np.eye(4)), float)
    targets = list(pose.config.targets)
    valid = dict.fromkeys(targets, 0)
    rows = []
    for (_, seconds, stamp), markers in zip(frames, detections):
        head = lookup(int(stamp)) if lookup and stamp else None
        if markers is None:
            observation = {x: {"valid": False, "reason": "image_decode_failed"} for x in targets}
        elif vio_enabled and head is None:
            observation = {
                x: {"valid": False, "reason": "missing_head_pose_or_calibration"} for x in targets
            }
        else:
            observation = pose.observe(None, intrinsic, head, imu_from_rgb, seconds, markers)
        for x in targets:
            valid[x] += bool(observation[x].get("valid"))
        rows.append(("pose", "arms", seconds, stamp, json.dumps(observation)))

    temporary = path / (OFFLINE_DB + ".tmp")
    temporary.unlink(missing_ok=True)
    with sqlite3.connect(temporary) as db:
        db.execute("CREATE TABLE samples(kind TEXT, name TEXT, t REAL, stamp_ns INTEGER, payload BLOB)")
        db.executemany("INSERT INTO samples VALUES (?,?,?,?,?)", rows)
        db.execute("CREATE INDEX sample_time ON samples(kind,name,t)")
    temporary.replace(path / OFFLINE_DB)
    info = {
        "created_unix_ns": time.time_ns(),
        "corner_refinement": OFFLINE_REFINEMENT,
        "frames": len(frames),
        "valid_ratios": {x: round(valid[x] / len(frames), 4) for x in targets},
        "elapsed_s": round(time.monotonic() - started, 1),
    }
    with settings_lock:
        stored = _read(path)
        # Recomputing is how one asks for the offline track, so switch to it;
        # the live track stays one click away.
        stored.update(offline=info, source="offline")
        write_json(path / SETTINGS, stored)
    return info
