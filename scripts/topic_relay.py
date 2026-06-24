#!/usr/bin/env python3

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Type

import rclpy
from geometry_msgs.msg import PoseStamped
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import CameraInfo, CompressedImage, Image, Imu
from std_msgs.msg import String

from camera_setup import enabled_cameras, load_setup, relay_config, relay_topic


def make_qos(depth: int = 10, reliable: bool = True) -> QoSProfile:
    return QoSProfile(
        reliability=ReliabilityPolicy.RELIABLE if reliable else ReliabilityPolicy.BEST_EFFORT,
        durability=DurabilityPolicy.VOLATILE,
        history=HistoryPolicy.KEEP_LAST,
        depth=depth,
    )


@dataclass(frozen=True)
class RelaySpec:
    source: str
    target: str
    msg_type: Type
    source_reliable: bool = False
    target_reliable: bool = True
    depth: int = 10


def _camera_topic(namespace: str, suffix: str) -> str:
    return f"/{namespace}/camera/{suffix.strip('/')}"


def build_relay_specs(config: Dict) -> List[RelaySpec]:
    specs: Dict[str, RelaySpec] = {}
    relay_settings = relay_config(config)
    if not relay_settings["enabled"]:
        return []

    def add(
        namespace: str,
        suffix: str,
        msg_type: Type,
        source_reliable: bool = False,
        target_reliable: bool = True,
        depth: int = 10,
    ) -> None:
        source = _camera_topic(namespace, suffix)
        target = relay_topic(source, config)
        specs[source] = RelaySpec(
            source=source,
            target=target,
            msg_type=msg_type,
            source_reliable=source_reliable,
            target_reliable=target_reliable,
            depth=depth,
        )

    for camera in enabled_cameras(config):
        namespace = str(camera.get("namespace") or camera.get("name") or "").strip("/")
        if not namespace:
            continue

        for stream in ("color", "infra1", "infra2"):
            add(namespace, f"{stream}/camera_info", CameraInfo, depth=2)
            add(namespace, f"{stream}/image_raw", Image, depth=1)
            add(namespace, f"{stream}/image_rect_raw", Image, depth=1)
        add(namespace, "color/image_raw/compressed", CompressedImage, depth=1)
        add(namespace, "color/image_rect_raw/compressed", CompressedImage, depth=1)
        add(namespace, "imu", Imu, depth=50)
        add(namespace, "vio_100hz", PoseStamped, depth=50)
        add(namespace, "vio_pose_synced", PoseStamped, depth=50)
        add(namespace, "vio_status", String, depth=10)

    return sorted(specs.values(), key=lambda item: item.source)


class InsightTopicRelay(Node):
    def __init__(self, config_path: Path) -> None:
        super().__init__("insight_topic_relay")
        self.config_path = config_path
        self.config = load_setup(config_path)
        self.relay_settings = relay_config(self.config)
        self.publishers_by_source = {}
        self.subscriptions_by_source = []

        if not self.relay_settings["enabled"]:
            self.get_logger().warn("topic_relay.enabled is false; relay node will not subscribe")
            return

        specs = build_relay_specs(self.config)
        if not specs:
            self.get_logger().warn("no relay topics configured")
            return
        for spec in specs:
            source_qos = make_qos(depth=spec.depth, reliable=spec.source_reliable)
            target_qos = make_qos(depth=spec.depth, reliable=spec.target_reliable)
            publisher = self.create_publisher(spec.msg_type, spec.target, target_qos)
            self.publishers_by_source[spec.source] = publisher
            subscription = self.create_subscription(
                spec.msg_type,
                spec.source,
                self._make_callback(spec.source),
                source_qos,
            )
            self.subscriptions_by_source.append(subscription)
            self.get_logger().info(f"relay {spec.source} -> {spec.target}")

    def _make_callback(self, source: str):
        publisher = self.publishers_by_source[source]

        def callback(msg) -> None:
            publisher.publish(msg)

        return callback


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Relay Insight camera topics to a shared fan-out namespace.")
    parser.add_argument("--config", default=str(Path(__file__).resolve().parents[1] / "config" / "cameras.json"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config_path = Path(args.config).resolve()

    rclpy.init(args=None)
    node = InsightTopicRelay(config_path)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
