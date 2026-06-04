#!/usr/bin/env python3

import argparse
from pathlib import Path

import rclpy
from geometry_msgs.msg import PoseStamped
from nav_msgs.msg import Path as PathMsg
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
from rclpy.executors import ExternalShutdownException

from camera_setup import build_path_entries, load_setup


def make_qos(depth: int = 50) -> QoSProfile:
    return QoSProfile(
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.VOLATILE,
        history=HistoryPolicy.KEEP_LAST,
        depth=depth,
    )


class PoseToPathNode(Node):
    def __init__(self, config_path: Path) -> None:
        super().__init__("insight_pose_to_path")

        config = load_setup(config_path)

        self.max_path_length = int(config.get("max_path_length", 3000))
        self.path_publishers = {}
        self.paths = {}
        self.pose_subscriptions = []

        qos = make_qos()
        for camera in build_path_entries(config):
            name = camera["name"]
            pose_topic = camera["pose_topic"]
            path_topic = camera["path_topic"]

            self.paths[name] = PathMsg()
            self.path_publishers[name] = self.create_publisher(PathMsg, path_topic, qos)

            subscription = self.create_subscription(
                PoseStamped,
                pose_topic,
                self._make_pose_callback(name),
                qos,
            )
            self.pose_subscriptions.append(subscription)
            self.get_logger().info(
                f"Tracking {name}: pose={pose_topic} -> path={path_topic}"
            )

    def _make_pose_callback(self, camera_name: str):
        def callback(msg: PoseStamped) -> None:
            path_msg = self.paths[camera_name]
            path_msg.header = msg.header
            path_msg.poses.append(msg)

            if len(path_msg.poses) > self.max_path_length:
                path_msg.poses = path_msg.poses[-self.max_path_length :]

            self.path_publishers[camera_name].publish(path_msg)

        return callback


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parent.parent / "config" / "cameras.json"),
    )
    args = parser.parse_args()

    rclpy.init()
    node = PoseToPathNode(Path(args.config))
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
