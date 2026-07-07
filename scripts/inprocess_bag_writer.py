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

import queue
import threading
from typing import Optional, Set

import rosbag2_py
from rclpy.serialization import serialize_message


def ros_type_string(msg: object) -> str:
    cls = type(msg)
    package = cls.__module__.split(".")[0]
    return f"{package}/msg/{cls.__name__}"


class InProcessBagWriter:
    """Background-thread rosbag2 writer fed via a bounded queue so the ROS
    callback thread that calls write() never blocks on disk I/O.
    """

    def __init__(self, output_path: str, storage_id: str = "sqlite3", max_queue: int = 128) -> None:
        self._queue: "queue.Queue[object]" = queue.Queue(maxsize=max_queue)
        self._topics_created: Set[str] = set()
        self._dropped = 0
        self._stop_sentinel = object()
        self._started = threading.Event()
        self._open_error: Optional[str] = None
        self._thread = threading.Thread(
            target=self._run, args=(output_path, storage_id), daemon=True, name="inprocess_bag_writer"
        )
        self._thread.start()
        if not self._started.wait(timeout=5.0):
            raise RuntimeError(f"Timed out opening rosbag writer at {output_path}")
        if self._open_error:
            raise RuntimeError(self._open_error)

    def write(self, topic: str, msg: object, stamp_ns: int) -> None:
        try:
            self._queue.put_nowait((topic, msg, stamp_ns))
        except queue.Full:
            self._dropped += 1

    @property
    def dropped_count(self) -> int:
        return self._dropped

    def close(self, timeout: float = 10.0) -> None:
        self._queue.put(self._stop_sentinel)
        self._thread.join(timeout=timeout)

    def _run(self, output_path: str, storage_id: str) -> None:
        try:
            writer = rosbag2_py.SequentialWriter()
            writer.open(
                rosbag2_py.StorageOptions(uri=output_path, storage_id=storage_id),
                rosbag2_py.ConverterOptions("", ""),
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the constructor via _open_error
            self._open_error = str(exc)
            self._started.set()
            return
        self._started.set()

        while True:
            item = self._queue.get()
            if item is self._stop_sentinel:
                break
            topic, msg, stamp_ns = item
            if topic not in self._topics_created:
                writer.create_topic(
                    rosbag2_py.TopicMetadata(name=topic, type=ros_type_string(msg), serialization_format="cdr")
                )
                self._topics_created.add(topic)
            writer.write(topic, serialize_message(msg), stamp_ns)
        # rosbag2_py has no explicit close(); metadata.yaml is only flushed
        # when the writer object is destroyed.
        del writer
