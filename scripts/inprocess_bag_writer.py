#!/usr/bin/env python3

"""Write already-received ROS messages straight into a rosbag2 bag, in the
same process that already subscribes to them for the live dashboard view.

Why this exists: `ros2 bag record` opens its own independent DDS reader for
every topic it records. For the big image topics, that means two RELIABLE
readers (the dashboard's own display subscription + the recorder's) end up
attached to the same publisher at once. Measured on this fleet, that second
reader triggers a backpressure stall on the publisher's reliable writer
history that starves *both* readers -- image topics dropped to ~3% of native
rate during recording while every other (small-message) topic stayed near
full rate, and an isolated recorder with zero other readers hit full native
rate every time. Routing the already-received message straight to disk here
means recording never adds a second reader for these topics at all.
"""

import multiprocessing
import queue
import threading
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
    """Own sqlite/rosbag2 writes outside the dashboard interpreter/GIL."""
    try:
        writer = rosbag2_py.SequentialWriter()
        storage_options = rosbag2_py.StorageOptions(uri=output_path, storage_id=storage_id)
        if storage_config_uri:
            storage_options.storage_config_uri = storage_config_uri
        writer.open(storage_options, rosbag2_py.ConverterOptions("", ""))
    except Exception as exc:  # pragma: no cover - surfaced to parent startup
        errors.put(str(exc))
        ready.set()
        return
    ready.set()
    topics_created = set()
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
    del writer


class InProcessBagWriter:
    """One DDS subscription, with serialization and sqlite writes off its callback.

    Serialization remains in a local background thread because it consumes
    rclpy message objects directly. The expensive rosbag2/SQLite calls run in
    a spawned process, so they cannot monopolize the dashboard's GIL and
    delay the image subscription callback.
    """

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
        if not self._storage_errors.empty():
            raise RuntimeError(self._storage_errors.get())
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="inprocess_bag_serializer"
        )
        self._thread.start()

    def write(self, topic: str, msg: object, stamp_ns: int) -> None:
        try:
            self._queue.put_nowait((topic, msg, stamp_ns))
        except queue.Full:
            with self._drop_lock:
                self._dropped += 1
                self._dropped_by_topic[topic] = self._dropped_by_topic.get(topic, 0) + 1

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
        self._storage_entries.put(None)
        self._storage_process.join(timeout=timeout)
        if self._storage_process.is_alive():
            self._storage_process.terminate()
            self._storage_process.join(timeout=1.0)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is self._stop_sentinel:
                break
            topic, msg, stamp_ns = item
            self._storage_entries.put((topic, ros_type_string(msg), serialize_message(msg), stamp_ns))
