#!/usr/bin/env python3

"""Write dashboard-received image messages without adding another DDS reader."""

import multiprocessing
import queue
import threading
import time
from typing import Dict, Optional

import rosbag2_py
from rclpy.serialization import serialize_message


def ros_type_string(msg: object) -> str:
    cls = type(msg)
    package = cls.__module__.split(".")[0]
    return f"{package}/msg/{cls.__name__}"


def _storage_writer_process(
    output_path: str, storage_id: str, storage_config_uri: str,
    entries: "multiprocessing.queues.Queue", ready: object, errors: object,
) -> None:
    """Own rosbag2 storage writes outside the dashboard interpreter/GIL."""
    writer = None
    try:
        writer = rosbag2_py.SequentialWriter()
        storage_options = rosbag2_py.StorageOptions(uri=output_path, storage_id=storage_id)
        if storage_config_uri:
            storage_options.storage_config_uri = storage_config_uri
        writer.open(storage_options, rosbag2_py.ConverterOptions("", ""))
    except Exception as exc:  # pragma: no cover - surfaced to parent
        errors.put(f"cannot open rosbag writer: {exc}")
        ready.set()
        return
    ready.set()
    topics_created = set()
    try:
        while True:
            item = entries.get()
            if item is None:
                break
            topic, topic_type, serialized, stamp_ns = item
            if topic not in topics_created:
                writer.create_topic(
                    rosbag2_py.TopicMetadata(name=topic, type=topic_type, serialization_format="cdr")
                )
                topics_created.add(topic)
            writer.write(topic, serialized, stamp_ns)
    except Exception as exc:  # pragma: no cover - surfaced by close()
        try:
            errors.put_nowait(f"rosbag write failed: {exc}")
        except queue.Full:
            pass
    finally:
        del writer


class InProcessBagWriter:
    """Serialize in a thread and write through rosbag2 in a spawned process."""

    def __init__(
        self,
        output_path: str,
        storage_id: str = "sqlite3",
        max_queue: int = 128,
        storage_config_uri: str = "",
    ) -> None:
        self._storage_config_uri = storage_config_uri
        self._queue: "queue.Queue[object]" = queue.Queue(maxsize=max_queue)
        self._dropped = 0
        self._dropped_by_topic: Dict[str, int] = {}
        self._drop_lock = threading.Lock()
        self._failure_lock = threading.Lock()
        self._failure: Optional[str] = None
        self._stop_sentinel = object()
        context = multiprocessing.get_context("spawn")
        self._storage_entries = context.Queue(maxsize=max_queue)
        self._storage_ready = context.Event()
        self._storage_errors = context.Queue(maxsize=1)
        self._storage_process = context.Process(
            target=_storage_writer_process,
            args=(output_path, storage_id, storage_config_uri, self._storage_entries,
                  self._storage_ready, self._storage_errors),
            daemon=True,
            name="rosbag_storage_writer",
        )
        self._storage_process.start()
        if not self._storage_ready.wait(timeout=5.0):
            self._storage_process.terminate()
            raise RuntimeError(f"Timed out opening rosbag writer at {output_path}")
        try:
            startup_error = self._storage_errors.get_nowait()
        except queue.Empty:
            startup_error = None
        if startup_error:
            self._storage_process.join(timeout=1.0)
            raise RuntimeError(startup_error)
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="inprocess_bag_serializer"
        )
        self._thread.start()

    def write(self, topic: str, msg: object, stamp_ns: int) -> None:
        self._capture_storage_error()
        if self._failure is not None or not self._storage_process.is_alive():
            self._record_drop(topic)
            return
        try:
            self._queue.put_nowait((topic, msg, stamp_ns))
        except queue.Full:
            self._record_drop(topic)

    def _record_drop(self, topic: str) -> None:
        with self._drop_lock:
            self._dropped += 1
            self._dropped_by_topic[topic] = self._dropped_by_topic.get(topic, 0) + 1

    def _capture_storage_error(self) -> None:
        try:
            error = self._storage_errors.get_nowait()
        except queue.Empty:
            return
        with self._failure_lock:
            if self._failure is None:
                self._failure = str(error)

    @property
    def dropped_count(self) -> int:
        with self._drop_lock:
            return self._dropped

    @property
    def dropped_by_topic(self) -> Dict[str, int]:
        with self._drop_lock:
            return dict(self._dropped_by_topic)

    def close(self, timeout: float = 60.0) -> None:
        self._queue.put(self._stop_sentinel)
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            self._storage_process.terminate()
            self._storage_process.join(timeout=1.0)
            raise RuntimeError(
                f"Timed out draining image serialization queue after {timeout:.0f}s"
            )
        sentinel_deadline = time.monotonic() + timeout
        while self._storage_process.is_alive():
            try:
                self._storage_entries.put(None, timeout=0.25)
                break
            except queue.Full:
                if time.monotonic() >= sentinel_deadline:
                    self._storage_process.terminate()
                    self._storage_process.join(timeout=1.0)
                    raise RuntimeError(
                        f"Timed out signaling image rosbag finalization after {timeout:.0f}s"
                    )
        self._storage_process.join(timeout=timeout)
        if self._storage_process.is_alive():
            self._storage_process.terminate()
            self._storage_process.join(timeout=1.0)
            raise RuntimeError(
                f"Timed out finalizing image rosbag after {timeout:.0f}s"
            )
        self._capture_storage_error()
        if self._failure is not None:
            raise RuntimeError(self._failure)
        if self._storage_process.exitcode != 0:
            raise RuntimeError(
                f"Image rosbag writer exited with code {self._storage_process.exitcode}"
            )

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is self._stop_sentinel:
                break
            topic, msg, stamp_ns = item
            try:
                entry = (topic, ros_type_string(msg), serialize_message(msg), stamp_ns)
            except Exception as exc:  # noqa: BLE001 - surfaced when recording stops
                with self._failure_lock:
                    if self._failure is None:
                        self._failure = f"message serialization failed: {exc}"
                continue
            while True:
                self._capture_storage_error()
                if self._failure is not None or not self._storage_process.is_alive():
                    self._record_drop(topic)
                    break
                try:
                    self._storage_entries.put(entry, timeout=0.25)
                    break
                except queue.Full:
                    continue
