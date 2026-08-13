"""Prepare deterministic browser playback artifacts from a ROS 2 bag."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Deque, Dict, Iterable, Optional
from urllib.parse import quote

import cv2
import numpy as np
from scipy.spatial.transform import Rotation, Slerp

from hand_tracking.extract_gripper import decode_color_image
from .playback import _read_bag_topics


SCHEMA_VERSION = 4
PLAYBACK_FPS = 30.0
PLAYBACK_MAX_DIMENSION = 720
REVIEW_WIDTH = 1280
REVIEW_HEIGHT = 720
REVIEW_BITRATE = 6_000_000
MAX_TRAJECTORY_POINTS = 600


class _Cancelled(RuntimeError):
    pass


class _RecordingStarted(_Cancelled):
    pass


class _FfmpegWriter:
    def __init__(self, path: Path, shape: tuple[int, int], fps: float) -> None:
        height, width = shape
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise RuntimeError("ffmpeg is required for prepared playback")
        self.frames = 0
        self.encoder_name = "libx264"
        self._process = subprocess.Popen(
            [
                ffmpeg,
                "-y",
                "-loglevel",
                "error",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "bgr24",
                "-s:v",
                f"{width}x{height}",
                "-r",
                f"{fps:g}",
                "-i",
                "pipe:0",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "fastdecode",
                "-crf",
                "20",
                "-g",
                str(round(fps)),
                "-keyint_min",
                str(round(fps)),
                "-sc_threshold",
                "0",
                "-threads",
                "4",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    @property
    def process(self) -> subprocess.Popen:
        return self._process

    def write(self, frame: np.ndarray) -> None:
        if frame.ndim == 2:
            frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        if frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8:
            raise ValueError(f"unsupported video frame shape: {frame.shape}")
        assert self._process.stdin is not None
        try:
            self._process.stdin.write(np.ascontiguousarray(frame).tobytes())
        except BrokenPipeError as exc:
            raise RuntimeError(self._error_text() or "ffmpeg stopped early") from exc
        self.frames += 1

    def close(self) -> None:
        if self._process.stdin is not None and not self._process.stdin.closed:
            self._process.stdin.close()
        return_code = self._process.wait()
        if return_code:
            raise RuntimeError(self._error_text() or f"ffmpeg exited with {return_code}")

    def abort(self) -> None:
        if self._process.poll() is None:
            self._process.kill()
            self._process.wait()

    def _error_text(self) -> str:
        if self._process.stderr is None:
            return ""
        return self._process.stderr.read().decode("utf-8", errors="replace").strip()


class _GstH264Writer:
    """Jetson hardware H.264 MP4 writer with the same interface as ffmpeg."""

    def __init__(self, path: Path, shape: tuple[int, int], fps: float) -> None:
        try:
            import gi

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
        except Exception as exc:  # pragma: no cover - host-only development
            raise RuntimeError("GStreamer Python bindings are unavailable") from exc
        if not Gst.is_initialized():
            Gst.init(None)
        required = ("appsrc", "videoconvert", "nvvidconv", "nvv4l2h264enc", "h264parse", "qtmux")
        missing = [name for name in required if Gst.ElementFactory.find(name) is None]
        if missing:
            raise RuntimeError(f"missing GStreamer review elements: {', '.join(missing)}")
        height, width = shape
        location = str(path).replace("\\", "\\\\").replace('"', '\\"')
        description = (
            f"appsrc name=src is-live=false format=time block=true "
            f"caps=video/x-raw,format=BGR,width={width},height={height},framerate={round(fps)}/1 ! "
            "videoconvert ! video/x-raw,format=I420 ! "
            "nvvidconv ! video/x-raw(memory:NVMM),format=NV12 ! "
            f"nvv4l2h264enc bitrate={REVIEW_BITRATE} insert-sps-pps=true "
            f"idrinterval={round(fps)} iframeinterval={round(fps)} maxperf-enable=true ! "
            "h264parse config-interval=-1 ! qtmux faststart=true ! "
            f"filesink location=\"{location}\""
        )
        self._Gst = Gst
        self._pipeline = Gst.parse_launch(description)
        self._source = self._pipeline.get_by_name("src")
        self._bus = self._pipeline.get_bus()
        self._closed = False
        self.frames = 0
        self.encoder_name = "nvv4l2h264enc"
        if self._pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self._pipeline.set_state(Gst.State.NULL)
            raise RuntimeError("Jetson review encoder refused to start")

    def write(self, frame: np.ndarray) -> None:
        if frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8:
            raise ValueError(f"unsupported video frame shape: {frame.shape}")
        self._raise_bus_error()
        payload = np.ascontiguousarray(frame).tobytes()
        buffer = self._Gst.Buffer.new_allocate(None, len(payload), None)
        buffer.fill(0, payload)
        duration = int(self._Gst.SECOND / PLAYBACK_FPS)
        buffer.pts = self.frames * duration
        buffer.dts = buffer.pts
        buffer.duration = duration
        if self._source.emit("push-buffer", buffer) != self._Gst.FlowReturn.OK:
            self._raise_bus_error()
            raise RuntimeError("Jetson review encoder rejected a frame")
        self.frames += 1

    def close(self) -> None:
        if self._closed:
            return
        self._source.emit("end-of-stream")
        message = self._bus.timed_pop_filtered(
            30 * self._Gst.SECOND,
            self._Gst.MessageType.ERROR | self._Gst.MessageType.EOS,
        )
        try:
            if message is None:
                raise RuntimeError("Jetson review encoder timed out while finalizing MP4")
            if message.type == self._Gst.MessageType.ERROR:
                error, debug = message.parse_error()
                raise RuntimeError(f"Jetson review encoder failed: {error} ({debug or 'no details'})")
        finally:
            self._pipeline.set_state(self._Gst.State.NULL)
            self._closed = True

    def abort(self) -> None:
        if not self._closed:
            self._pipeline.set_state(self._Gst.State.NULL)
            self._closed = True

    def _raise_bus_error(self) -> None:
        message = self._bus.timed_pop_filtered(0, self._Gst.MessageType.ERROR)
        if message is None:
            return
        error, debug = message.parse_error()
        raise RuntimeError(f"Jetson review encoder failed: {error} ({debug or 'no details'})")


def _video_writer(path: Path, shape: tuple[int, int], fps: float):
    if os.environ.get("INSIGHT_REVIEW_SOFTWARE_ENCODER", "").strip() != "1":
        try:
            return _GstH264Writer(path, shape, fps)
        except Exception:  # noqa: BLE001 - any unavailable Jetson path uses libx264
            pass
    return _FfmpegWriter(path, shape, fps)


def _message_pose(message: object) -> tuple[np.ndarray, np.ndarray]:
    pose = message.pose.pose if hasattr(message.pose, "pose") else message.pose
    position = np.asarray(
        [pose.position.x, pose.position.y, pose.position.z], dtype=np.float64
    )
    quaternion = np.asarray(
        [
            pose.orientation.x,
            pose.orientation.y,
            pose.orientation.z,
            pose.orientation.w,
        ],
        dtype=np.float64,
    )
    norm = float(np.linalg.norm(quaternion))
    if not np.isfinite(norm) or norm <= 1e-9:
        raise ValueError("pose contains an invalid quaternion")
    return position, quaternion / norm


def _nearest_indices(source: np.ndarray, target: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    right = np.clip(np.searchsorted(source, target, side="left"), 0, len(source) - 1)
    left = np.clip(right - 1, 0, len(source) - 1)
    choose_right = np.abs(source[right] - target) < np.abs(source[left] - target)
    indices = np.where(choose_right, right, left)
    skew_ms = np.abs(source[indices] - target) / 1e6
    return indices.astype(np.int64), skew_ms


def _playback_frame(frame: np.ndarray) -> np.ndarray:
    """Bound decode cost while retaining more detail than the camera dock displays."""
    height, width = frame.shape[:2]
    largest = max(height, width)
    if largest <= PLAYBACK_MAX_DIMENSION:
        return frame
    scale = PLAYBACK_MAX_DIMENSION / largest
    output_width = max(2, int(round(width * scale)) // 2 * 2)
    output_height = max(2, int(round(height * scale)) // 2 * 2)
    return cv2.resize(
        frame,
        (output_width, output_height),
        interpolation=cv2.INTER_AREA,
    )


def _review_cells(
    count: int, width: int = REVIEW_WIDTH, height: int = REVIEW_HEIGHT
) -> list[dict[str, int]]:
    """Return stable cells for one review surface; three cameras stay side by side."""
    if count < 1:
        return []
    if count <= 3:
        columns, rows = count, 1
    else:
        columns = int(math.ceil(math.sqrt(count)))
        rows = int(math.ceil(count / columns))
    cells = []
    for index in range(count):
        column, row = index % columns, index // columns
        x0 = round(column * width / columns)
        x1 = round((column + 1) * width / columns)
        y0 = round(row * height / rows)
        y1 = round((row + 1) * height / rows)
        cells.append({"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0})
    return cells


def _ensure_bgr(frame: np.ndarray) -> np.ndarray:
    if frame.ndim == 2:
        return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    if frame.ndim != 3:
        raise ValueError(f"unsupported review frame shape: {frame.shape}")
    if frame.shape[2] == 4:
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
    if frame.shape[2] != 3:
        raise ValueError(f"unsupported review frame shape: {frame.shape}")
    return frame


def _compose_review_frame(
    frames: list[np.ndarray],
    cameras: list[dict[str, object]],
    duplicate_flags: list[bool],
    frame_index: int,
    fps: float = PLAYBACK_FPS,
    width: int = REVIEW_WIDTH,
    height: int = REVIEW_HEIGHT,
) -> np.ndarray:
    """Letterbox labeled camera frames into one browser-friendly review surface."""
    if len(frames) != len(cameras) or len(frames) != len(duplicate_flags):
        raise ValueError("review frame, camera, and duplicate counts must match")
    canvas = np.full((height, width, 3), (18, 18, 18), dtype=np.uint8)
    for frame, camera, duplicate, cell in zip(
        frames, cameras, duplicate_flags, _review_cells(len(frames), width, height)
    ):
        frame = _ensure_bgr(frame)
        inset = 4
        header = 34
        available_width = max(2, cell["width"] - inset * 2)
        available_height = max(2, cell["height"] - header - inset * 2)
        scale = min(available_width / frame.shape[1], available_height / frame.shape[0])
        output_width = max(2, int(frame.shape[1] * scale) & ~1)
        output_height = max(2, int(frame.shape[0] * scale) & ~1)
        resized = cv2.resize(
            frame,
            (output_width, output_height),
            interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR,
        )
        x = cell["x"] + (cell["width"] - output_width) // 2
        y = cell["y"] + header + (available_height - output_height) // 2
        canvas[y : y + output_height, x : x + output_width] = resized
        label = str(camera.get("label") or camera.get("name") or "camera")
        cv2.putText(
            canvas,
            label,
            (cell["x"] + 10, cell["y"] + 24),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.52,
            (235, 235, 235),
            1,
            cv2.LINE_AA,
        )
        if duplicate:
            text_size = cv2.getTextSize("REPEAT", cv2.FONT_HERSHEY_SIMPLEX, 0.48, 1)[0]
            cv2.putText(
                canvas,
                "REPEAT",
                (cell["x"] + cell["width"] - text_size[0] - 10, cell["y"] + 24),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.48,
                (60, 160, 255),
                1,
                cv2.LINE_AA,
            )
        if cell["x"]:
            cv2.line(
                canvas,
                (cell["x"], cell["y"]),
                (cell["x"], cell["y"] + cell["height"]),
                (72, 72, 72),
                1,
            )
    seconds = frame_index / max(float(fps), 1.0)
    timecode = f"{int(seconds // 60):02d}:{seconds % 60:06.3f}  #{frame_index + 1}"
    size = cv2.getTextSize(timecode, cv2.FONT_HERSHEY_SIMPLEX, 0.44, 1)[0]
    cv2.rectangle(canvas, (width - size[0] - 16, height - 27), (width, height), (0, 0, 0), -1)
    cv2.putText(
        canvas,
        timecode,
        (width - size[0] - 8, height - 9),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.44,
        (225, 225, 225),
        1,
        cv2.LINE_AA,
    )
    return canvas


def _source_signature(bag_path: Path) -> list[dict[str, object]]:
    files = [bag_path / "metadata.yaml", *sorted(bag_path.glob("*.db3"))]
    return [
        {
            "name": item.name,
            "size": item.stat().st_size,
            "mtime_ns": item.stat().st_mtime_ns,
        }
        for item in files
        if item.is_file()
    ]


def _cache_key(
    signature: list[dict[str, object]], configuration: dict[str, object]
) -> str:
    identity = {
        "schema_version": SCHEMA_VERSION,
        "playback_fps": PLAYBACK_FPS,
        "max_dimension": PLAYBACK_MAX_DIMENSION,
        "review_size": [REVIEW_WIDTH, REVIEW_HEIGHT],
        "source_signature": signature,
        "configuration": configuration,
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def _select_recorded_streams(
    recorded_topics: Iterable[str],
    cameras: list[dict[str, object]],
    poses: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Keep configured playback streams that are actually present in the bag."""
    topics = {str(topic) for topic in recorded_topics}
    selected_cameras = [
        dict(camera) for camera in cameras if str(camera.get("topic", "")) in topics
    ]
    if not selected_cameras:
        raise ValueError("bag contains no configured camera image topic for playback")
    selected_poses = [
        dict(pose) for pose in poses if str(pose.get("topic", "")) in topics
    ]
    return selected_cameras, selected_poses


def _open_reader(bag_path: Path, topics: Iterable[str]):
    import rosbag2_py

    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(bag_path), storage_id=""),
        rosbag2_py.ConverterOptions(
            input_serialization_format="cdr", output_serialization_format="cdr"
        ),
    )
    topic_types = {item.name: item.type for item in reader.get_all_topics_and_types()}
    reader.set_filter(rosbag2_py.StorageFilter(topics=list(topics)))
    return reader, topic_types


class PreparedPlaybackManager:
    """Build and cache fixed-rate video plus pose timelines before playback."""

    def __init__(self, rosbag_root: Path, cache_root: Path) -> None:
        self.rosbag_root = rosbag_root.resolve()
        self.cache_root = cache_root.resolve()
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._writer = None
        self._state = "idle"
        self._bag_name = ""
        self._progress = 0.0
        self._stage = ""
        self._error = ""
        self._cache_key = ""
        self._browser_stats: dict[str, object] = {}
        self._background_active = False
        self._recording_manager = None
        self._configuration_provider: Optional[
            Callable[[], tuple[list[dict[str, object]], list[dict[str, object]]]]
        ] = None
        self._queue: Deque[str] = deque()
        self._queued_names: set[str] = set()
        self._queue_errors: dict[str, str] = {}
        self._queue_condition = threading.Condition(self._lock)
        self._scheduler_stop = threading.Event()
        self._scheduler_thread: Optional[threading.Thread] = None

    def status(self) -> Dict[str, object]:
        with self._lock:
            return self._status_unlocked()

    def _status_unlocked(self) -> Dict[str, object]:
        payload: Dict[str, object] = {
            "state": self._state,
            "bag_name": self._bag_name,
            "progress": round(self._progress, 3),
            "stage": self._stage,
            "error": self._error,
            "background": self._background_active,
            "queued": list(self._queue),
            "queue_errors": dict(self._queue_errors),
        }
        if self._browser_stats:
            payload["browser_stats"] = dict(self._browser_stats)
        if self._state in {"ready", "playing"} and self._bag_name:
            payload["manifest_url"] = (
                f"{self.manifest_url(self._bag_name)}?v={self._cache_key}"
            )
        return payload

    def prepare(
        self,
        bag_name: str,
        recording_manager,
        cameras: list[dict[str, object]],
        poses: list[dict[str, object]],
        *,
        background: bool = False,
    ) -> Dict[str, object]:
        bag_path = self._bag_path(bag_name)
        stopping_thread = None
        with self._lock:
            if self._state == "playing":
                raise RuntimeError("Stop the active playback before preparing another bag.")
            if self._thread is not None and self._thread.is_alive():
                if self._state == "preparing" and self._bag_name == bag_name:
                    return self._status_unlocked()
                if not background and self._background_active:
                    self._cancel.set()
                    stopping_thread = self._thread
                else:
                    raise RuntimeError("Previous playback preparation is still stopping.")
        if stopping_thread is not None:
            stopping_thread.join(timeout=3.0)
            if stopping_thread.is_alive():
                raise RuntimeError("Background review preparation is still stopping; retry shortly.")
        with recording_manager._lock:
            recording_manager._cleanup_if_exited_unlocked()
            if recording_manager.processes:
                raise RuntimeError("Cannot prepare playback while recording is active.")
        cameras, poses = _select_recorded_streams(
            _read_bag_topics(bag_path), cameras, poses
        )
        signature = _source_signature(bag_path)
        configuration = {"cameras": cameras, "poses": poses}
        if self._cache_valid(bag_name, signature, configuration):
            cached = json.loads(
                (self._review_dir(bag_name) / "manifest.json").read_text(encoding="utf-8")
            )
            with self._lock:
                self._state = "ready"
                self._bag_name = bag_name
                self._progress = 1.0
                self._stage = "Prepared playback cache ready"
                self._error = ""
                self._cache_key = str(cached.get("cache_key", ""))
                self._browser_stats = {}
            return self.status()
        with self._lock:
            if self._state == "preparing":
                if self._bag_name == bag_name:
                    return self._status_unlocked()
                raise RuntimeError(f"Already preparing {self._bag_name}.")
            self._cancel.clear()
            self._state = "preparing"
            self._bag_name = bag_name
            self._progress = 0.0
            self._stage = "Scanning bag timelines"
            self._error = ""
            self._cache_key = ""
            self._browser_stats = {}
            self._background_active = background
            self._recording_manager = recording_manager
            self._thread = threading.Thread(
                target=self._run,
                args=(bag_name, bag_path, signature, configuration, cameras, poses),
                daemon=True,
                name="prepared_playback",
            )
            self._thread.start()
        return self.status()

    def configure_background(
        self,
        recording_manager,
        configuration_provider: Callable[
            [], tuple[list[dict[str, object]], list[dict[str, object]]]
        ],
    ) -> None:
        """Generate newly completed review bundles away from the recording path."""
        with self._queue_condition:
            self._recording_manager = recording_manager
            self._configuration_provider = configuration_provider
            if self._scheduler_thread is not None:
                return
            self._scheduler_thread = threading.Thread(
                target=self._scheduler_loop,
                daemon=True,
                name="review_prebuild_scheduler",
            )
            self._scheduler_thread.start()

    def enqueue(self, bag_name: str) -> Dict[str, object]:
        self._bag_path(bag_name)
        with self._queue_condition:
            if bag_name != self._bag_name and bag_name not in self._queued_names:
                self._queue.append(bag_name)
                self._queued_names.add(bag_name)
                self._queue_errors.pop(bag_name, None)
                self._queue_condition.notify_all()
            return self._status_unlocked()

    def enqueue_all(self) -> Dict[str, object]:
        for path in sorted(self.rosbag_root.iterdir(), key=lambda item: item.stat().st_mtime):
            if path.is_dir() and (path / "metadata.yaml").is_file():
                self.enqueue(path.name)
        return self.status()

    def shutdown(self) -> None:
        self._scheduler_stop.set()
        self.stop()
        with self._queue_condition:
            self._queue_condition.notify_all()
        scheduler = self._scheduler_thread
        if scheduler is not None and scheduler is not threading.current_thread():
            scheduler.join(timeout=3.0)

    def _scheduler_loop(self) -> None:
        while not self._scheduler_stop.is_set():
            with self._queue_condition:
                while not self._queue and not self._scheduler_stop.is_set():
                    self._queue_condition.wait(timeout=1.0)
                if self._scheduler_stop.is_set():
                    return
                bag_name = self._queue.popleft()
                self._queued_names.discard(bag_name)
                recording_manager = self._recording_manager
                provider = self._configuration_provider
            if recording_manager is None or provider is None:
                continue
            if self._recording_busy(recording_manager):
                self._requeue_after_pause(bag_name)
                continue
            try:
                cameras, poses = provider()
                status = self.prepare(
                    bag_name,
                    recording_manager,
                    cameras,
                    poses,
                    background=True,
                )
            except (OSError, ValueError) as exc:
                with self._lock:
                    self._queue_errors[bag_name] = str(exc)
                continue
            except RuntimeError as exc:
                if self._recording_busy(recording_manager) or self._state in {
                    "playing",
                    "preparing",
                }:
                    self._requeue_after_pause(bag_name, delay=1.0)
                else:
                    with self._lock:
                        self._queue_errors[bag_name] = str(exc)
                continue
            if status.get("state") == "ready":
                continue
            while not self._scheduler_stop.is_set():
                with self._lock:
                    thread = self._thread
                if thread is None or not thread.is_alive():
                    break
                if self._recording_busy(recording_manager):
                    self._cancel.set()
                thread.join(timeout=0.25)
            state = str(self.status().get("state", "idle"))
            if state not in {"ready", "error"} and not self._scheduler_stop.is_set():
                self._requeue_after_pause(bag_name)

    def _requeue_after_pause(self, bag_name: str, delay: float = 0.5) -> None:
        if self._scheduler_stop.wait(delay):
            return
        with self._queue_condition:
            if bag_name not in self._queued_names:
                self._queue.appendleft(bag_name)
                self._queued_names.add(bag_name)
            self._queue_condition.wait(timeout=1.0)

    @staticmethod
    def _recording_busy(recording_manager) -> bool:
        return recording_manager.is_recording() or recording_manager.merge_state == "merging"

    def activate(self, bag_name: str) -> Dict[str, object]:
        with self._lock:
            if self._state != "ready" or self._bag_name != bag_name:
                raise RuntimeError("Prepared playback is not ready.")
            self._state = "playing"
            self._stage = "Playing prepared media"
        return self.status()

    def record_browser_stats(self, payload: dict[str, object]) -> None:
        with self._lock:
            self._browser_stats = {**payload, "updated_monotonic": time.monotonic()}

    def stop(self) -> Dict[str, object]:
        with self._lock:
            self._cancel.set()
            writer = self._writer
            self._state = "idle"
            self._bag_name = ""
            self._progress = 0.0
            self._stage = ""
            self._error = ""
            self._cache_key = ""
            self._browser_stats = {}
        if writer is not None:
            writer.abort()
        return self.status()

    def artifact_path(self, bag_name: str, filename: str) -> Path:
        if Path(filename).name != filename:
            raise ValueError("invalid playback artifact")
        review_root = self._review_dir(bag_name)
        path = (review_root / filename).resolve()
        if not path.is_relative_to(review_root) or not path.is_file():
            raise FileNotFoundError(filename)
        return path

    @staticmethod
    def manifest_url(bag_name: str) -> str:
        return f"/api/playback/artifacts/{quote(bag_name, safe='')}/manifest.json"

    def _bag_path(self, bag_name: str) -> Path:
        if not bag_name or Path(bag_name).name != bag_name:
            raise ValueError("Invalid bag name.")
        path = (self.rosbag_root / bag_name).resolve()
        if not path.is_relative_to(self.rosbag_root) or not path.is_dir():
            raise ValueError(f"Bag not found: {bag_name}")
        return path

    def _cache_valid(
        self,
        bag_name: str,
        signature: list[dict[str, object]],
        configuration: dict[str, object],
    ) -> bool:
        manifest_path = self._review_dir(bag_name) / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return (
            manifest.get("schema_version") == SCHEMA_VERSION
            and manifest.get("source_signature") == signature
            and manifest.get("configuration") == configuration
            and manifest.get("cache_key") == _cache_key(signature, configuration)
            and bool(manifest.get("cameras"))
            and all(
                (manifest_path.parent / camera["video_file"]).is_file()
                for camera in manifest.get("cameras", [])
            )
        )

    def _review_dir(self, bag_name: str) -> Path:
        return self._bag_path(bag_name) / "review"

    def _set_progress(self, progress: float, stage: str) -> None:
        with self._lock:
            if self._state == "preparing":
                self._progress = min(0.999, max(0.0, progress))
                self._stage = stage

    def _check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise _Cancelled()
        recording_manager = self._recording_manager
        if recording_manager is not None and recording_manager.is_recording():
            raise _RecordingStarted()

    def _run(
        self,
        bag_name: str,
        bag_path: Path,
        signature: list[dict[str, object]],
        configuration: dict[str, object],
        cameras: list[dict[str, object]],
        poses: list[dict[str, object]],
    ) -> None:
        temporary = bag_path / ".review.preparing"
        final = bag_path / "review"
        previous = bag_path / ".review.previous"
        try:
            if previous.exists() and not final.exists():
                os.replace(previous, final)
            elif previous.exists():
                shutil.rmtree(previous)
            if temporary.exists():
                shutil.rmtree(temporary)
            temporary.mkdir(parents=True)
            scan = self._scan(bag_path, cameras, poses)
            self._check_cancelled()
            image_stamps = scan["image_stamps"]
            start_ns = max(values[0] for values in image_stamps.values())
            end_ns = min(values[-1] for values in image_stamps.values())
            frame_count = int(np.floor((end_ns - start_ns) * PLAYBACK_FPS / 1e9)) + 1
            if frame_count < 2:
                raise ValueError("camera streams do not share a usable playback interval")
            targets = start_ns + np.rint(
                np.arange(frame_count, dtype=np.float64) * 1e9 / PLAYBACK_FPS
            ).astype(np.int64)
            selections = {}
            cache_key = _cache_key(signature, configuration)
            source_camera_manifest = []
            cells = _review_cells(len(cameras))
            for camera, cell in zip(cameras, cells):
                name = str(camera["name"])
                indices, skew_ms = _nearest_indices(image_stamps[name], targets)
                selections[name] = indices
                source_camera_manifest.append(
                    {
                        **camera,
                        "source_frames": int(len(image_stamps[name])),
                        "duplicate_frames": int(np.count_nonzero(np.diff(indices) == 0)),
                        "max_skew_ms": round(float(np.max(skew_ms)), 3),
                        "tile": cell,
                    }
                )
            self._set_progress(0.1, "Encoding synchronized review at 30 fps")
            shape, count, encoder, source_dimensions = self._encode_review_video(
                bag_path,
                cameras,
                selections,
                temporary / "review.mp4",
                lambda review_frames: self._set_progress(
                    0.1 + 0.75 * review_frames / frame_count,
                    "Encoding synchronized review at 30 fps",
                ),
            )
            if count != frame_count:
                raise RuntimeError(f"review encoded {count}/{frame_count} frames")
            for entry in source_camera_manifest:
                dimensions = source_dimensions.get(str(entry["name"]))
                if dimensions:
                    entry["source_width"] = int(dimensions[1])
                    entry["source_height"] = int(dimensions[0])
            self._set_progress(0.88, "Interpolating complete 3D trajectories")
            pose_manifest = self._pose_manifest(scan["poses"], poses, targets)
            total_duplicates = sum(
                int(item["duplicate_frames"]) for item in source_camera_manifest
            )
            max_skew_ms = max(
                float(item["max_skew_ms"]) for item in source_camera_manifest
            )
            pose_coverage = {
                str(item["name"]): round(
                    sum(bool(value) for value in item.get("valid", [])) / frame_count,
                    6,
                )
                for item in pose_manifest
            }
            quality_state = (
                "pass"
                if total_duplicates == 0
                and max_skew_ms <= 20.0
                and {str(item["name"]) for item in cameras}.issubset(pose_coverage)
                and all(value >= 0.99 for value in pose_coverage.values())
                else "warning"
            )
            review_camera = {
                "name": "review",
                "label": "Synchronized camera review",
                "role": "review",
                "rotation_deg": 0,
                "row": 0,
                "column": 0,
                "video_file": "review.mp4",
                "video_url": (
                    f"/api/playback/artifacts/{quote(bag_name, safe='')}/"
                    f"review.mp4?v={cache_key}"
                ),
                "width": int(shape[1]),
                "height": int(shape[0]),
            }
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "bag_name": bag_name,
                "cache_key": cache_key,
                "source_signature": signature,
                "configuration": configuration,
                "fps": PLAYBACK_FPS,
                "frame_count": frame_count,
                "duration_s": round(frame_count / PLAYBACK_FPS, 6),
                "start_stamp_ns": int(start_ns),
                "master_camera": "review",
                "cameras": [review_camera],
                "source_cameras": source_camera_manifest,
                "poses": pose_manifest,
                "review": {
                    "width": int(shape[1]),
                    "height": int(shape[0]),
                    "encoder": encoder,
                    "layout": "tiled",
                },
                "quality": {
                    "state": quality_state,
                    "duplicate_frames": total_duplicates,
                    "max_skew_ms": round(max_skew_ms, 3),
                    "pose_coverage": pose_coverage,
                },
            }
            (temporary / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            self._check_cancelled()
            if final.exists():
                os.replace(final, previous)
            try:
                os.replace(temporary, final)
            except Exception:
                if previous.exists() and not final.exists():
                    os.replace(previous, final)
                raise
            if previous.exists():
                shutil.rmtree(previous)
            with self._lock:
                if self._state == "preparing" and self._bag_name == bag_name:
                    self._state = "ready"
                    self._progress = 1.0
                    self._stage = "Prepared playback cache ready"
                    self._cache_key = cache_key
        except _RecordingStarted:
            with self._lock:
                if self._bag_name == bag_name:
                    self._state = "idle"
                    self._stage = "Review generation paused for recording"
                    self._bag_name = ""
        except _Cancelled:
            with self._lock:
                if self._bag_name == bag_name and self._state == "preparing":
                    self._state = "idle"
                    self._stage = ""
                    self._bag_name = ""
        except Exception as exc:  # noqa: BLE001 - surface background failure to UI
            with self._lock:
                if self._bag_name == bag_name:
                    self._state = "error"
                    self._error = str(exc)
                    self._stage = "Playback preparation failed"
        finally:
            with self._lock:
                self._writer = None
                self._background_active = False
                if self._thread is threading.current_thread():
                    self._thread = None
                self._queue_condition.notify_all()
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    def _scan(
        self,
        bag_path: Path,
        cameras: list[dict[str, object]],
        poses: list[dict[str, object]],
    ) -> dict[str, object]:
        from rclpy.serialization import deserialize_message
        from rosidl_runtime_py.utilities import get_message

        image_by_topic = {str(item["topic"]): str(item["name"]) for item in cameras}
        pose_by_topic = {str(item["topic"]): str(item["name"]) for item in poses}
        topics = [*image_by_topic, *pose_by_topic]
        reader, topic_types = _open_reader(bag_path, topics)
        missing = [topic for topic in topics if topic not in topic_types]
        if missing:
            raise ValueError(f"bag is missing playback topics: {', '.join(missing)}")
        classes = {topic: get_message(topic_types[topic]) for topic in topics}
        image_stamps: dict[str, list[int]] = {name: [] for name in image_by_topic.values()}
        pose_data = {
            name: {"stamps": [], "positions": [], "quaternions": []}
            for name in pose_by_topic.values()
        }
        while reader.has_next():
            self._check_cancelled()
            topic, raw, record_stamp = reader.read_next()
            if topic in image_by_topic:
                image_stamps[image_by_topic[topic]].append(int(record_stamp))
            else:
                message = deserialize_message(raw, classes[topic])
                position, quaternion = _message_pose(message)
                target = pose_data[pose_by_topic[topic]]
                target["stamps"].append(int(record_stamp))
                target["positions"].append(position)
                target["quaternions"].append(quaternion)
        normalized_images = {}
        for name, values in image_stamps.items():
            array = np.asarray(values, dtype=np.int64)
            if len(array) < 2 or np.any(np.diff(array) <= 0):
                raise ValueError(f"{name} image timestamps are not strictly increasing")
            normalized_images[name] = array
        normalized_poses = {}
        for name, values in pose_data.items():
            stamps = np.asarray(values["stamps"], dtype=np.int64)
            if len(stamps) < 2:
                raise ValueError(f"{name} has fewer than two pose samples")
            order = np.argsort(stamps)
            stamps = stamps[order]
            unique = np.concatenate(([True], np.diff(stamps) > 0))
            normalized_poses[name] = (
                stamps[unique],
                np.asarray(values["positions"], dtype=np.float64)[order][unique],
                np.asarray(values["quaternions"], dtype=np.float64)[order][unique],
            )
        self._set_progress(0.1, "Bag timelines scanned")
        return {"image_stamps": normalized_images, "poses": normalized_poses}

    def _encode_review_video(
        self,
        bag_path: Path,
        cameras: list[dict[str, object]],
        selections: dict[str, np.ndarray],
        output: Path,
        progress,
    ) -> tuple[tuple[int, int], int, str, dict[str, tuple[int, int]]]:
        from rclpy.serialization import deserialize_message
        from rosidl_runtime_py.utilities import get_message

        camera_by_topic = {str(item["topic"]): item for item in cameras}
        topics = list(camera_by_topic)
        reader, topic_types = _open_reader(bag_path, topics)
        message_classes = {topic: get_message(topic_types[topic]) for topic in topics}
        wanted: dict[str, dict[int, list[int]]] = {}
        for camera in cameras:
            name = str(camera["name"])
            selected: dict[int, list[int]] = {}
            for target_index, source_index in enumerate(selections[name]):
                selected.setdefault(int(source_index), []).append(target_index)
            wanted[name] = selected
        source_indices = {str(item["name"]): 0 for item in cameras}
        source_dimensions: dict[str, tuple[int, int]] = {}
        pending: list[dict[str, np.ndarray]] = [dict() for _ in range(len(next(iter(selections.values()))))]
        writer = None
        shape = (REVIEW_HEIGHT, REVIEW_WIDTH)
        next_target = 0
        try:
            writer = _video_writer(output, shape, PLAYBACK_FPS)
            with self._lock:
                self._writer = writer
            while reader.has_next():
                self._check_cancelled()
                current_topic, raw, _stamp = reader.read_next()
                camera = camera_by_topic.get(current_topic)
                if camera is None:
                    continue
                name = str(camera["name"])
                source_index = source_indices[name]
                source_indices[name] += 1
                target_indices = wanted[name].get(source_index)
                if not target_indices:
                    continue
                message = deserialize_message(raw, message_classes[current_topic])
                frame = decode_color_image(message, topic_types[current_topic])
                if frame is None:
                    raise RuntimeError(f"failed to decode {current_topic} frame {source_index}")
                frame = _ensure_bgr(frame)
                current_shape = (int(frame.shape[0]), int(frame.shape[1]))
                previous_shape = source_dimensions.setdefault(name, current_shape)
                if current_shape != previous_shape:
                    raise RuntimeError(f"{current_topic} resolution changed during the bag")
                for target_index in target_indices:
                    pending[target_index][name] = frame
                while next_target < len(pending) and len(pending[next_target]) == len(cameras):
                    self._check_cancelled()
                    ordered_frames = [pending[next_target][str(item["name"])] for item in cameras]
                    duplicate_flags = [
                        next_target > 0
                        and selections[str(item["name"])][next_target]
                        == selections[str(item["name"])][next_target - 1]
                        for item in cameras
                    ]
                    review = _compose_review_frame(
                        ordered_frames,
                        cameras,
                        duplicate_flags,
                        next_target,
                    )
                    writer.write(review)
                    pending[next_target].clear()
                    next_target += 1
                    if writer.frames % 10 == 0:
                        progress(writer.frames)
            if next_target != len(pending):
                missing = sorted(
                    set(str(item["name"]) for item in cameras) - set(pending[next_target])
                )
                raise RuntimeError(
                    f"review stopped at {next_target}/{len(pending)} frames; missing {', '.join(missing)}"
                )
            writer.close()
            return shape, writer.frames, writer.encoder_name, source_dimensions
        except Exception:
            if writer is not None:
                writer.abort()
            raise
        finally:
            with self._lock:
                self._writer = None

    def _pose_manifest(
        self,
        samples: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]],
        poses: list[dict[str, object]],
        targets: np.ndarray,
    ) -> list[dict[str, object]]:
        result = []
        for pose in poses:
            name = str(pose["name"])
            stamps, positions, quaternions = samples[name]
            clipped = np.clip(targets, stamps[0], stamps[-1])
            output_positions = np.column_stack(
                [np.interp(clipped, stamps, positions[:, axis]) for axis in range(3)]
            )
            origin = int(stamps[0])
            rotations = Slerp(
                (stamps - origin).astype(np.float64) / 1e9,
                Rotation.from_quat(quaternions),
            )(
                (clipped - origin).astype(np.float64) / 1e9
            ).as_quat()
            nearest = _nearest_indices(stamps, targets)[1]
            valid = (
                (targets >= stamps[0])
                & (targets <= stamps[-1])
                & (nearest <= 100.0)
            )
            valid_indices = np.flatnonzero(valid)
            if len(valid_indices) > MAX_TRAJECTORY_POINTS:
                selected = np.rint(
                    np.linspace(0, len(valid_indices) - 1, MAX_TRAJECTORY_POINTS)
                ).astype(np.int64)
                valid_indices = valid_indices[selected]
            trajectory = output_positions[valid_indices]
            result.append(
                {
                    **pose,
                    "positions": np.round(output_positions, 5).tolist(),
                    "quaternions_xyzw": np.round(rotations, 5).tolist(),
                    "valid": valid.tolist(),
                    "trajectory": np.round(trajectory, 4).tolist(),
                }
            )
        return result
