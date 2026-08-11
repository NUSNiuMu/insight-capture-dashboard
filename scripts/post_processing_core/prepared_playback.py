"""Prepare deterministic browser playback artifacts from a ROS 2 bag."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Dict, Iterable, Optional
from urllib.parse import quote

import cv2
import numpy as np
from scipy.spatial.transform import Rotation, Slerp

from hand_tracking.extract_gripper import decode_color_image
from .playback import _read_bag_topics


SCHEMA_VERSION = 3
PLAYBACK_FPS = 30.0
PLAYBACK_MAX_DIMENSION = 720
MAX_TRAJECTORY_POINTS = 600


class _Cancelled(RuntimeError):
    pass


class _FfmpegWriter:
    def __init__(self, path: Path, shape: tuple[int, int], fps: float) -> None:
        height, width = shape
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise RuntimeError("ffmpeg is required for prepared playback")
        self.frames = 0
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
        self._process: Optional[subprocess.Popen] = None
        self._state = "idle"
        self._bag_name = ""
        self._progress = 0.0
        self._stage = ""
        self._error = ""
        self._cache_key = ""

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
        }
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
    ) -> Dict[str, object]:
        bag_path = self._bag_path(bag_name)
        with self._lock:
            if self._state == "playing":
                raise RuntimeError("Stop the active playback before preparing another bag.")
            if self._thread is not None and self._thread.is_alive():
                if self._state == "preparing" and self._bag_name == bag_name:
                    return self._status_unlocked()
                raise RuntimeError("Previous playback preparation is still stopping.")
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
                (self.cache_root / bag_name / "manifest.json").read_text(encoding="utf-8")
            )
            with self._lock:
                self._state = "ready"
                self._bag_name = bag_name
                self._progress = 1.0
                self._stage = "Prepared playback cache ready"
                self._error = ""
                self._cache_key = str(cached.get("cache_key", ""))
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
            self._thread = threading.Thread(
                target=self._run,
                args=(bag_name, bag_path, signature, configuration, cameras, poses),
                daemon=True,
                name="prepared_playback",
            )
            self._thread.start()
        return self.status()

    def activate(self, bag_name: str) -> Dict[str, object]:
        with self._lock:
            if self._state != "ready" or self._bag_name != bag_name:
                raise RuntimeError("Prepared playback is not ready.")
            self._state = "playing"
            self._stage = "Playing prepared media"
        return self.status()

    def stop(self) -> Dict[str, object]:
        with self._lock:
            self._cancel.set()
            process = self._process
            self._state = "idle"
            self._bag_name = ""
            self._progress = 0.0
            self._stage = ""
            self._error = ""
            self._cache_key = ""
        if process is not None and process.poll() is None:
            process.kill()
        return self.status()

    def artifact_path(self, bag_name: str, filename: str) -> Path:
        if Path(filename).name != filename:
            raise ValueError("invalid playback artifact")
        path = (self.cache_root / bag_name / filename).resolve()
        if not path.is_relative_to(self.cache_root) or not path.is_file():
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
        manifest_path = self.cache_root / bag_name / "manifest.json"
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

    def _set_progress(self, progress: float, stage: str) -> None:
        with self._lock:
            if self._state == "preparing":
                self._progress = min(0.999, max(0.0, progress))
                self._stage = stage

    def _check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise _Cancelled()

    def _run(
        self,
        bag_name: str,
        bag_path: Path,
        signature: list[dict[str, object]],
        configuration: dict[str, object],
        cameras: list[dict[str, object]],
        poses: list[dict[str, object]],
    ) -> None:
        temporary = self.cache_root / f".{bag_name}.preparing"
        final = self.cache_root / bag_name
        try:
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
            camera_manifest = []
            for camera in cameras:
                name = str(camera["name"])
                indices, skew_ms = _nearest_indices(image_stamps[name], targets)
                selections[name] = indices
                camera_manifest.append(
                    {
                        **camera,
                        "video_file": f"{name}.mp4",
                        "video_url": (
                            f"/api/playback/artifacts/{quote(bag_name, safe='')}/"
                            f"{quote(name, safe='')}.mp4?v={cache_key}"
                        ),
                        "source_frames": int(len(image_stamps[name])),
                        "duplicate_frames": int(np.count_nonzero(np.diff(indices) == 0)),
                        "max_skew_ms": round(float(np.max(skew_ms)), 3),
                    }
                )
            total_output_frames = frame_count * len(cameras)
            written = 0
            for camera in cameras:
                self._check_cancelled()
                name = str(camera["name"])
                stage = f"Encoding {camera.get('label', name)} at 30 fps"
                shape, count = self._encode_video(
                    bag_path,
                    str(camera["topic"]),
                    selections[name],
                    temporary / f"{name}.mp4",
                    lambda camera_frames: self._set_progress(
                        0.1 + 0.75 * (written + camera_frames) / total_output_frames,
                        stage,
                    ),
                )
                written += count
                entry = next(item for item in camera_manifest if item["name"] == name)
                entry["width"] = int(shape[1])
                entry["height"] = int(shape[0])
                if count != frame_count:
                    raise RuntimeError(f"{name} encoded {count}/{frame_count} frames")
            self._set_progress(0.88, "Interpolating complete 3D trajectories")
            pose_manifest = self._pose_manifest(scan["poses"], poses, targets)
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
                "master_camera": next(
                    (str(item["name"]) for item in cameras if item.get("role") == "head"),
                    str(cameras[0]["name"]),
                ),
                "cameras": camera_manifest,
                "poses": pose_manifest,
            }
            (temporary / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            self._check_cancelled()
            if final.exists():
                shutil.rmtree(final)
            os.replace(temporary, final)
            with self._lock:
                if self._state == "preparing" and self._bag_name == bag_name:
                    self._state = "ready"
                    self._progress = 1.0
                    self._stage = "Prepared playback cache ready"
                    self._cache_key = cache_key
        except _Cancelled:
            pass
        except Exception as exc:  # noqa: BLE001 - surface background failure to UI
            with self._lock:
                if self._bag_name == bag_name:
                    self._state = "error"
                    self._error = str(exc)
                    self._stage = "Playback preparation failed"
        finally:
            with self._lock:
                self._process = None
                if self._thread is threading.current_thread():
                    self._thread = None
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
            message = deserialize_message(raw, classes[topic])
            if topic in image_by_topic:
                image_stamps[image_by_topic[topic]].append(int(record_stamp))
            else:
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

    def _encode_video(
        self,
        bag_path: Path,
        topic: str,
        source_indices: np.ndarray,
        output: Path,
        progress,
    ) -> tuple[tuple[int, int], int]:
        from rclpy.serialization import deserialize_message
        from rosidl_runtime_py.utilities import get_message

        reader, topic_types = _open_reader(bag_path, [topic])
        message_class = get_message(topic_types[topic])
        wanted: dict[int, int] = {}
        for source_index in source_indices:
            key = int(source_index)
            wanted[key] = wanted.get(key, 0) + 1
        writer: Optional[_FfmpegWriter] = None
        shape: Optional[tuple[int, int]] = None
        try:
            source_index = 0
            while reader.has_next():
                self._check_cancelled()
                current_topic, raw, _stamp = reader.read_next()
                repeat = wanted.get(source_index, 0)
                source_index += 1
                if current_topic != topic or repeat == 0:
                    continue
                message = deserialize_message(raw, message_class)
                frame = decode_color_image(message, topic_types[topic])
                if frame is None:
                    raise RuntimeError(f"failed to decode {topic} frame {source_index - 1}")
                if frame.ndim == 2:
                    frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
                frame = _playback_frame(frame)
                current_shape = (int(frame.shape[0]), int(frame.shape[1]))
                if writer is None:
                    shape = current_shape
                    writer = _FfmpegWriter(output, shape, PLAYBACK_FPS)
                    with self._lock:
                        self._process = writer.process
                elif current_shape != shape:
                    raise RuntimeError(f"{topic} resolution changed during the bag")
                for _ in range(repeat):
                    self._check_cancelled()
                    writer.write(frame)
                    if writer.frames % 10 == 0:
                        progress(writer.frames)
            if writer is None or shape is None:
                raise RuntimeError(f"no selected frames decoded for {topic}")
            writer.close()
            return shape, writer.frames
        except Exception:
            if writer is not None:
                writer.abort()
            raise
        finally:
            with self._lock:
                self._process = None

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
