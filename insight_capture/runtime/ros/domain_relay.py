"""Relay configured device topics between ROS domains without rewriting clocks."""

import argparse
import json
import os
import signal
import time
import multiprocessing
from pathlib import Path

from insight_capture.core.config import dashboard_streams, enabled_cameras
from insight_capture.runtime.discovery import camera_links
from insight_capture.runtime.ros.network import local_ipv4_addresses


def _active_sources(groups):
    addresses = local_ipv4_addresses()
    links = camera_links()
    active = {}
    for source, cameras in groups.items():
        required = {camera["relay_host_address"] for camera in cameras if camera.get("relay_host_address")}
        if required <= addresses:
            active[source] = frozenset(link for link in links if link.address in required)
    return active


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
    last_data, states = {}, {}

    def forward(publisher, topic):
        def receive(data):
            publisher.publish(data)
            last_data[topic] = time.monotonic()
        return receive

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
                callback = publisher.publish
                if kind.endswith("Image") or topic.endswith("/vio_100hz"):
                    last_data[topic] = None
                    callback = forward(publisher, topic)
                node.create_subscription(cls, topic, callback, qos, raw=True)
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
        next_check = time.monotonic() + 2
        while context.ok():
            executor.spin_once(timeout_sec=.1)
            now = time.monotonic()
            if now < next_check:
                continue
            next_check = now + 2
            for topic, last in last_data.items():
                publishers = node.count_publishers(topic)
                if last is not None and now - last <= 2:
                    state = "forwarding"
                elif publishers:
                    state = "publishers visible, no recent samples"
                else:
                    state = "no source publisher discovered"
                if states.get(topic) != state:
                    print(f"ROS domain {source} -> {domain} {topic}: {state} (source publishers={publishers})",
                          flush=True)
                    states[topic] = state
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
    for source, cameras in groups.items():
        required = sorted({camera["relay_host_address"] for camera in cameras if camera.get("relay_host_address")})
        if required:
            print(f"Source domain {source} requires local interfaces at {', '.join(required)}", flush=True)
    try:
        while True:
            active = _active_sources(groups)
            for source, (worker, links) in list(workers.items()):
                if active.get(source) == links:
                    if worker.exitcode is not None:
                        raise RuntimeError(f"Device relay exited unexpectedly: {worker.exitcode}")
                    continue
                del workers[source]
                if worker.is_alive():
                    worker.terminate()
                worker.join(timeout=6)
                if worker.is_alive():
                    worker.kill()
                    worker.join()
                reason = "changed" if source in active else "absent"
                print(f"Source domain {source} USB link {reason}; relay disposed", flush=True)
            for source in active.keys() - workers.keys():
                worker = context.Process(target=_relay_domain, args=(domain, source, groups[source]))
                worker.start()
                workers[source] = (worker, active[source])
            # Disposing a removed link also withdraws its target-domain locators.
            time.sleep(.5)
    except KeyboardInterrupt:
        pass
    finally:
        for worker, _ in workers.values():
            if worker.is_alive():
                worker.terminate()
        for worker, _ in workers.values():
            worker.join(timeout=6)
            if worker.is_alive():
                worker.kill()
                worker.join()


if __name__ == "__main__":
    main()
