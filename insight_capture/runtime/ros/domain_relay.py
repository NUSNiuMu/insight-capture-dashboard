"""Relay configured device topics between ROS domains without rewriting clocks."""

import argparse
import json
import os
import signal
from pathlib import Path

from insight_capture.core.config import dashboard_streams, enabled_cameras


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    # Background shells may inherit SIGINT ignored; own shutdown across contexts.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    config = json.loads(args.config.read_text())
    domain = int(config["ros_domain_id"])
    os.environ["ROS_DOMAIN_ID"] = str(domain)
    import rclpy
    from rclpy.context import Context
    from rclpy.signals import SignalHandlerOptions
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import QoSProfile, DurabilityPolicy, qos_profile_sensor_data
    from rosidl_runtime_py.utilities import get_message
    from tf2_msgs.msg import TFMessage

    contexts, nodes, executors = [], [], []
    import threading
    workers = []
    output_context = Context()
    rclpy.init(context=output_context, domain_id=domain, signal_handler_options=SignalHandlerOptions.NO)
    contexts.append(output_context)
    output = rclpy.create_node("device_domain_relay_output", context=output_context)
    nodes.append(output)
    groups = {}
    for camera in enabled_cameras(config):
        source = int(camera.get("ros_domain_id", domain))
        if source != domain:
            groups.setdefault(source, []).append(camera)
    try:
        for source, cameras in groups.items():
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
                    node.create_subscription(cls, topic, publisher.publish, qos)
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
            worker = threading.Thread(target=executor.spin, daemon=True)
            worker.start()
            workers.append(worker)
            print(f"Relaying device topics from domain {source} to {domain}", flush=True)
        executor = SingleThreadedExecutor(context=output_context)
        executor.add_node(output)
        executors.append(executor)
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        for executor in executors:
            executor.shutdown(timeout_sec=2)
        for worker in workers:
            worker.join(timeout=2)
        for node in nodes:
            node.destroy_node()
        for context in contexts:
            rclpy.try_shutdown(context=context)


if __name__ == "__main__":
    main()
