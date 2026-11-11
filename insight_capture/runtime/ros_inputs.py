"""One renewable ROS participant for camera inputs, retaining application state."""

import os
import threading
import traceback

import rclpy
from rclpy.context import Context
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node

from insight_capture.runtime.discovery import camera_links
from insight_capture.runtime.ros.network import refresh_ros_network


class CameraInputs:
    def __init__(self, owner):
        self.owner = owner
        self.domain = owner.context.get_domain_id()
        self.specs = []
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.generation = 0
        self.thread = None
        self.links = camera_links()
        self._create_node()

    def _create_node(self):
        refresh_ros_network()
        self.context = Context()
        self.context.init(domain_id=self.domain)
        self.node = Node(self.owner.get_name() + "_inputs", context=self.context)
        self.executor = SingleThreadedExecutor(context=self.context)
        self.executor.add_node(self.node)

    def create_subscription(self, *args, **kwargs):
        self.specs.append((args, kwargs))
        return self.node.create_subscription(*args, **kwargs)

    def _spin(self):
        try:
            self.executor.spin()
        except Exception:
            if not self.closed.is_set():
                self.owner.get_logger().fatal("Camera input executor failed:\n" + traceback.format_exc())
                os._exit(1)

    def _start_executor(self):
        self.thread = threading.Thread(target=self._spin, name="camera_inputs", daemon=True)
        self.thread.start()

    def start(self):
        self._start_executor()
        threading.Thread(target=self._monitor, name="camera_network", daemon=True).start()

    def _stop_executor(self):
        self.executor.shutdown()
        if self.thread is not None:
            self.thread.join()
        self.node.destroy_node()
        self.context.try_shutdown()

    def refresh(self, links):
        with self.lock:
            if self.closed.is_set():
                return
            self._stop_executor()
            self._create_node()
            for args, kwargs in self.specs:
                self.node.create_subscription(*args, **kwargs)
            self.generation += 1
            self.links = links
            self._start_executor()
            self.owner.events.changed("camera_transport", self.owner.get_name(), "reconnected",
                f"Input participant renewed, generation={self.generation}",
                ros_domain_id=self.domain, camera_links=[vars(link) for link in sorted(self.links, key=lambda x:x.name)])

    def _monitor(self):
        while not self.closed.wait(5.):
            try:
                links = camera_links()
            except OSError as exc:
                self.owner.events.changed("camera_transport", self.owner.get_name(), "scan_failed", str(exc), level="warning")
                continue
            if links != self.links:
                try:
                    self.refresh(links)
                except Exception:
                    self.owner.get_logger().fatal("Camera input reconnection failed:\n" + traceback.format_exc())
                    os._exit(1)

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        with self.lock:
            self._stop_executor()
