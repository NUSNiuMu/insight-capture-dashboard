"""Receive Ego stereo in its source domain; bridge bounded decoded pairs and VIO."""

import json
import multiprocessing
import os
import queue
import signal
import threading
import time

from .rig import stereo_views
from .synchronization import StereoPairSynchronizer


def put_latest(channel, value):
    """Replace a queued output without ever blocking capture callbacks."""
    try:
        channel.put_nowait(value)
        return 0
    except queue.Full:
        try:
            channel.get_nowait()
        except queue.Empty:
            return 1
        try:
            channel.put_nowait(value)
        except queue.Full:
            return 2
        return 1


class SourceSink:
    """Keep compressed capture and small pose forwarding independent of output DDS."""

    def __init__(self, config, channels):
        settings = config["egohalo_rectification"]
        camera = next(c for c in config["cameras"] if c["namespace"] == settings["namespace"])
        self.source_domain = int(camera.get("ros_domain_id", config["ros_domain_id"]))
        self.channels = channels
        self.views = {v["name"]: v for v in stereo_views(config)}
        owned = {stream["name"] for stream in camera.get("dashboard_streams", [])}
        if not self.views or any(view[side + "_source"] not in owned
                                 for view in self.views.values() for side in ("left", "right")):
            raise ValueError("Direct mapping requires stereo sources from one configured device")
        self.lock = threading.Lock()
        tolerance = int(settings.get("pair_tolerance_ms", 2.0) * 1e6)
        if not 0 <= tolerance < .5e9 / float(settings.get("source_fps", 30)):
            raise ValueError("Mapping pair tolerance must be less than half a source frame")
        self.syncs = {name: StereoPairSynchronizer(tolerance_ns=tolerance, queue_size=3)
                      for name in self.views}
        self.last_pairs = {}
        self.transforms = {}
        self.counts = dict(pairs=0, pair_output_dropped=0, stale_pairs=0,
                           vio=0, vio_output_dropped=0, resets=0)

    def attach(self, node, config):
        from geometry_msgs.msg import PoseStamped
        from rclpy.qos import QoSProfile, DurabilityPolicy, qos_profile_sensor_data
        from rclpy.serialization import serialize_message
        from tf2_msgs.msg import TFMessage

        self.serialize = serialize_message
        namespace = config["egohalo_rectification"]["namespace"]
        self.TFMessage = TFMessage

        def on_vio(message):
            self.counts["vio"] += 1
            self.counts["vio_output_dropped"] += put_latest(
                self.channels["vio"], (time.monotonic(), serialize_message(message)))

        def on_static(message):
            self.sendTransform([t for t in message.transforms
                                if t.child_frame_id.startswith(namespace + "_")])

        node.create_subscription(PoseStamped, f"/{namespace}/camera/vio_100hz",
                                 on_vio, qos_profile_sensor_data)
        node.create_subscription(TFMessage, "/tf_static", on_static,
            QoSProfile(depth=100, durability=DurabilityPolicy.TRANSIENT_LOCAL))

    def sendTransform(self, transforms):
        with self.lock:
            for transform in transforms:
                self.transforms[transform.child_frame_id] = transform
            if transforms:
                put_latest(self.channels["tf"], self.serialize(
                    self.TFMessage(transforms=list(self.transforms.values()))))

    def publish(self, message):
        # A bounded IPC queue can discard an update during startup; periodically
        # resend the complete static cache so geometry cannot remain missing.
        with self.lock:
            if self.transforms:
                put_latest(self.channels["tf"], self.serialize(
                    self.TFMessage(transforms=list(self.transforms.values()))))
        put_latest(self.channels["status"], self.serialize(message))

    def reset_frames(self):
        with self.lock:
            for sync in self.syncs.values():
                sync.clear()
            self.last_pairs.clear()
            self.counts["resets"] += 1

    def publish_frame(self, name, side, image, info):
        stamp = image.header.stamp.sec * 1_000_000_000 + image.header.stamp.nanosec
        with self.lock:
            sync = self.syncs[name]
            pair = (sync.push_left if side == "left" else sync.push_right)(stamp, (image, info))
            if pair is None:
                return
            view = self.views[name]
            # Frames with different geometry must never form a stereo observation.
            prefixes = [view[eye + "_frame"] + "_" for eye in ("left", "right")]
            if not all(payload[0].header.frame_id.startswith(prefix) and payload[0].header.frame_id != prefix
                       for prefix, payload in zip(prefixes, (pair.left, pair.right))):
                self.counts["stale_pairs"] += 1
                return
            identity = [payload[0].header.frame_id.removeprefix(view[eye + "_frame"] + "_")
                        for eye, payload in (("left", pair.left), ("right", pair.right))]
            if identity[0] != identity[1] or pair.stamp_ns <= self.last_pairs.get(name, -1):
                self.counts["stale_pairs"] += 1
                return
            self.last_pairs[name] = pair.stamp_ns
            messages = []
            for eye, (frame, calibration) in (("left", pair.left), ("right", pair.right)):
                messages.extend(((view[eye + "_info_topic"], self.serialize(calibration)),
                                 (view[eye + "_image_topic"], self.serialize(frame))))
            self.counts["pairs"] += 1
            self.counts["pair_output_dropped"] += put_latest(
                self.channels["pairs"], (time.monotonic(), messages))

    def stats(self):
        with self.lock:
            return dict(self.counts)


def _source_worker(config, channels, stop):
    # Spawn before output-domain ROS setup: its interface exclusions must not
    # hide the source USB NIC from this independent DDS participant.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    from .egohalo_rectifier import run_rectifier
    for channel in channels.values():
        channel.cancel_join_thread()
    run_rectifier(config, stop, SourceSink(config, channels))


def run_bridge(config):
    """Run an isolated source decoder and a small output-domain publisher."""
    ctx = multiprocessing.get_context("spawn")
    stop = ctx.Event()
    channels = {name: ctx.Queue(maxsize=size) for name, size in (
        ("pairs", 2), ("vio", 128), ("tf", 2), ("status", 2))}
    source_domain = SourceSink(config, {}).source_domain
    if source_domain == int(config["ros_domain_id"]):
        raise ValueError("Direct mapping requires distinct source and output domains")
    namespace = config["egohalo_rectification"]["namespace"]
    if config["mapping"]["vio_topic"] == f"/{namespace}/camera/vio_100hz":
        raise ValueError("Direct mapping requires a private output VIO topic")
    worker = ctx.Process(target=_source_worker, args=(config, channels, stop))
    worker.start()
    node = None
    interrupted = False
    try:
        os.environ["ROS_DOMAIN_ID"] = str(config["ros_domain_id"])
        from insight_capture.runtime.ros.network import configure_ros_network
        configure_ros_network(config)
        import rclpy
        from rclpy.serialization import deserialize_message
        from geometry_msgs.msg import PoseStamped
        from sensor_msgs.msg import CameraInfo, Image
        from std_msgs.msg import String
        from tf2_msgs.msg import TFMessage
        from tf2_ros import StaticTransformBroadcaster

        rclpy.init()
        node = rclpy.create_node("egohalo_mapping_bridge_output")
        publishers = {}
        for view in stereo_views(config):
            for side in ("left", "right"):
                for suffix, cls in (("_image_topic", Image), ("_info_topic", CameraInfo)):
                    topic = view[side + suffix]
                    publishers[topic] = node.create_publisher(cls, topic, 2)
        vio = node.create_publisher(PoseStamped, config["mapping"]["vio_topic"], 16)
        status = node.create_publisher(String,
            "/" + config["egohalo_rectification"]["namespace"] + "/rig/status", 2)
        broadcaster = StaticTransformBroadcaster(node)
        max_age = float(config["egohalo_rectification"].get("max_output_age_sec", .5))
        if max_age <= 0:
            raise ValueError("Mapping output maximum age must be positive")
        metrics = dict(expired_pairs=0, expired_vio=0, published_pairs=0, published_vio=0)

        def drain():
            # Separate channels prevent video publication from starving VIO.
            for name, limit in (("tf", 2), ("vio", 128), ("pairs", 2), ("status", 2)):
                for _ in range(limit):
                    try:
                        value = channels[name].get_nowait()
                    except queue.Empty:
                        break
                    if name in ("vio", "pairs"):
                        received, value = value
                        if time.monotonic() - received > max_age:
                            metrics["expired_" + name] += 1
                            continue
                    if name == "tf":
                        broadcaster.sendTransform(deserialize_message(value, TFMessage).transforms)
                    elif name == "vio":
                        vio.publish(value)
                        metrics["published_vio"] += 1
                    elif name == "pairs":
                        for topic, payload in value:
                            publishers[topic].publish(payload)
                        metrics["published_pairs"] += 1
                    else:
                        message = deserialize_message(value, String)
                        payload = json.loads(message.data)
                        payload.update(output=dict(metrics), source_domain=source_domain,
                                       output_domain=int(config["ros_domain_id"]))
                        message.data = json.dumps(payload)
                        status.publish(message)

        node.create_timer(.005, drain)
        while rclpy.ok() and not stop.is_set():
            if not worker.is_alive():
                worker.join()
                raise RuntimeError(f"Ego mapping source exited: {worker.exitcode}")
            rclpy.spin_once(node, timeout_sec=.02)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        stop.set()
        worker.join(timeout=5)
        if worker.is_alive():
            os.kill(worker.pid, signal.SIGINT)
            worker.join(timeout=3)
        if worker.is_alive():
            worker.kill()
            worker.join()
        if node is not None:
            node.destroy_node()
            rclpy.try_shutdown()
        for channel in channels.values():
            channel.close()
        if not interrupted and worker.exitcode not in (0, -signal.SIGINT):
            raise RuntimeError(f"Ego mapping source exited: {worker.exitcode}")
