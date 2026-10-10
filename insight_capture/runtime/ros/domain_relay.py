"""Relay configured device topics between ROS domains without rewriting clocks."""

import argparse
import json
import os
import signal
import time
import multiprocessing
from pathlib import Path

from insight_capture.core.config import dashboard_streams, enabled_cameras
from insight_capture.runtime.ros.network import local_ipv4_addresses


def relay_topics(camera):
    base = f"/{camera['namespace']}/camera"
    topics = {}
    for stream in dashboard_streams(camera):
        topics[stream["topic"]] = (
            "sensor_msgs/msg/Image" if stream["type"] == "image"
            else "sensor_msgs/msg/CompressedImage"
        )
        topics[stream["camera_info_topic"]] = "sensor_msgs/msg/CameraInfo"
    topics.update({
        base + "/vio_100hz": "geometry_msgs/msg/PoseStamped",
        base + "/vio_image": "geometry_msgs/msg/PoseStamped",
        base + "/imu": "sensor_msgs/msg/Imu",
        base + "/vio_status": "std_msgs/msg/String",
    })
    return topics


def _relay_domain(domain, source, cameras):
    """Give each source domain its own interpreter so high-rate streams cannot starve peers."""
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    os.environ["ROS_DOMAIN_ID"] = str(domain)
    import rclpy
    from rclpy.context import Context
    from rclpy.signals import SignalHandlerOptions
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import QoSProfile, DurabilityPolicy, qos_profile_sensor_data
    from rosidl_runtime_py.utilities import get_message
    from tf2_msgs.msg import TFMessage

    contexts, nodes, executors = [], [], []
    output_context = Context()
    rclpy.init(context=output_context, domain_id=domain, signal_handler_options=SignalHandlerOptions.NO)
    contexts.append(output_context)
    output = rclpy.create_node("device_domain_relay_output", context=output_context)
    nodes.append(output)
    try:
        context = Context()
        rclpy.init(context=context, domain_id=source, signal_handler_options=SignalHandlerOptions.NO)
        contexts.append(context)
        node = rclpy.create_node(f"device_domain_relay_{source}", context=context)
        nodes.append(node)
        for camera in cameras:
            for topic, kind in relay_topics(camera).items():
                cls = get_message(kind)
                publisher = output.create_publisher(cls, topic, 30)
                # Image publishers are reliable; preserve complete encoded AUs.
                qos = 30 if kind.endswith(("Image", "CameraInfo")) else qos_profile_sensor_data
                # Forward CDR bytes verbatim instead of copying image arrays through Python.
                node.create_subscription(cls, topic, publisher.publish, qos, raw=True)
        prefixes = tuple(str(c["namespace"]) + "_" for c in cameras)
        static_qos = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        publisher = output.create_publisher(TFMessage, "/tf_static", static_qos)
        cache = {}
        def forward_tf(message, prefixes=prefixes, publisher=publisher, cache=cache):
            for transform in message.transforms:
                if transform.child_frame_id.startswith(prefixes):
                    cache[transform.child_frame_id] = transform
            if cache:
                publisher.publish(TFMessage(transforms=list(cache.values())))
        node.create_subscription(
            TFMessage, "/tf_static", forward_tf,
            QoSProfile(depth=100, durability=DurabilityPolicy.TRANSIENT_LOCAL),
        )
        executor = SingleThreadedExecutor(context=context)
        executor.add_node(node)
        executors.append(executor)
        print(f"Relaying device topics from domain {source} to {domain}", flush=True)
        while context.ok():
            executor.spin_once(timeout_sec=.1)
    except KeyboardInterrupt:
        pass
    finally:
        for executor in executors:
            executor.shutdown(timeout_sec=2)
        for node in nodes:
            node.destroy_node()
        for context in contexts:
            rclpy.try_shutdown(context=context)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    config = json.loads(args.config.read_text())
    domain = int(config["ros_domain_id"])
    groups = {}
    for camera in enabled_cameras(config):
        source = int(camera.get("ros_domain_id", domain))
        if source != domain:
            groups.setdefault(source, []).append(camera)
    # Do not fork an initialized DDS runtime; children construct their own contexts.
    context = multiprocessing.get_context("spawn")
    workers = {}
    if not groups:
        return
    try:
        while True:
            addresses = local_ipv4_addresses()
            active = {
                source for source, cameras in groups.items()
                if all(not camera.get("relay_host_address")
                       or camera["relay_host_address"] in addresses for camera in cameras)
            }
            for source in set(workers) - active:
                worker = workers.pop(source)
                if worker.is_alive():
                    os.kill(worker.pid, signal.SIGINT)
                worker.join(timeout=6)
                if worker.is_alive():
                    worker.kill()
                    worker.join()
                print(f"Source domain {source} USB link absent; relay disposed", flush=True)
            for source, worker in workers.items():
                if worker.exitcode is not None:
                    raise RuntimeError(f"Device relay exited unexpectedly: {worker.exitcode}")
            for source in active - workers.keys():
                worker = context.Process(target=_relay_domain, args=(domain, source, groups[source]))
                worker.start()
                workers[source] = worker
            # Disposing a removed link also withdraws its target-domain locators.
            time.sleep(.5)
    except KeyboardInterrupt:
        pass
    finally:
        for worker in workers.values():
            if worker.is_alive():
                os.kill(worker.pid, signal.SIGINT)
        for worker in workers.values():
            worker.join(timeout=6)
            if worker.is_alive():
                worker.kill()
                worker.join()


if __name__ == "__main__":
    main()
