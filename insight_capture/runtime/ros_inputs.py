"""One renewable ROS participant for camera inputs, retaining application state."""

import os
import threading
import traceback

import rclpy
from rclpy.context import Context
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node

from insight_capture.runtime.discovery import camera_link_generations
from insight_capture.runtime.ros.network import camera_transport_links, refresh_ros_network


class CameraInputs:
    def __init__(self, owner):
        self.owner = owner
        self.domain = owner.context.get_domain_id()
        self.specs = []
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.generation = 0
        self.threads = []
        self.links = camera_transport_links()
        self._link_generations = camera_link_generations(self.links)
        self._create_node(self.links)

    def _create_node(self, links):
        refresh_ros_network(links)
        self.context = Context()
        self.context.init(domain_id=self.domain)
        self.node, self.executor = self._make_endpoint("_inputs")
        self.raw_input = None

    def _make_endpoint(self, suffix):
        node = Node(self.owner.get_name() + suffix, context=self.context)
        executor = SingleThreadedExecutor(context=self.context)
        executor.add_node(node)
        return node, executor

    def create_subscription(self, *args, **kwargs):
        subscription = self._subscribe(args, kwargs)
        self.specs.append((args, kwargs))
        return subscription

    def _subscribe(self, args, kwargs):
        if not kwargs.get("raw"):
            return self.node.create_subscription(*args, **kwargs)
        # Let ordered encoded frames drain even when raw image callbacks are busy.
        if self.raw_input is None:
            self.raw_input = self._make_endpoint("_encoded_inputs")
        return self.raw_input[0].create_subscription(*args, **kwargs)

    def published_topics(self, *, with_types=False):
        """Read the live input participant, including after a USB/IP renewal."""
        with self.lock:
            if self.closed.is_set():
                raise RuntimeError("Camera input participant is closed")
            topics = {name: types for name, types in self.node.get_topic_names_and_types()
                      if self.node.count_publishers(name) > 0}
            return topics if with_types else list(topics)

    def _spin(self, executor):
        try:
            executor.spin()
        except Exception:
            if not self.closed.is_set():
                self.owner.get_logger().fatal("Camera input executor failed:\n" + traceback.format_exc())
                os._exit(1)

    def _start_executor(self):
        for node, executor in self._endpoints():
            thread = threading.Thread(target=self._spin, args=(executor,), name=node.get_name(), daemon=True)
            self.threads.append(thread)
            thread.start()

    def _endpoints(self):
        yield self.node, self.executor
        if self.raw_input is not None:
            yield self.raw_input

    def start(self):
        self._start_executor()
        threading.Thread(target=self._monitor, name="camera_network", daemon=True).start()

    def _stop_executor(self):
        for _, executor in self._endpoints():
            executor.shutdown()
        for thread in self.threads:
            thread.join()
        self.threads.clear()
        for node, _ in self._endpoints():
            node.destroy_node()
        self.context.try_shutdown()

    def refresh(self, links):
        with self.lock:
            if self.closed.is_set():
                return
            self._stop_executor()
            self._create_node(links)
            for args, kwargs in self.specs:
                self._subscribe(args, kwargs)
            self.generation += 1
            self.links = links
            self._link_generations = camera_link_generations(links)
            self._start_executor()
            self.owner.events.changed("camera_transport", self.owner.get_name(), "reconnected",
                f"Input participant renewed, generation={self.generation}",
                ros_domain_id=self.domain, camera_links=[vars(link) for link in sorted(self.links, key=lambda x:x.name)])

    def _monitor(self):
        while not self.closed.wait(5.):
            try:
                links = camera_transport_links()
            except OSError as exc:
                self.owner.events.changed("camera_transport", self.owner.get_name(), "scan_failed", str(exc), level="warning")
                continue
            if camera_link_generations(links) != self._link_generations:
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
