#!/usr/bin/env python3
"""Snapshot live Insight9 factory TF into a separate board-evaluation config."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time

import numpy as np

from insight_capture.postprocess.quality.board_accuracy import load_config, rigid
from insight_capture.runtime.mapping.geometry import (
    left_to_stereo_center,
    matrix_from_transform,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--camera-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = load_config(args.config, evaluation=False)
    camera = config["camera"]
    snapshot_path = args.output.with_name(args.output.stem + "_tf.json")
    if args.output.exists() or snapshot_path.exists():
        raise ValueError("output config / TF snapshot already exists; use new paths")
    os.environ["ROS_DOMAIN_ID"] = str(
        json.loads(args.camera_config.read_text())["ros_domain_id"]
    )

    import rclpy
    from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
    from sensor_msgs.msg import CameraInfo
    from tf2_msgs.msg import TFMessage

    left, right, rgb = (
        camera["stereo_left_frame"],
        camera["stereo_right_frame"],
        camera["image_frame"],
    )
    required = {(left, right), (left, rgb)}
    transforms = {}
    infos = []

    def receive_tf(message):
        for item in message.transforms:
            key = (item.header.frame_id, item.child_frame_id)
            if key in required:
                t, q = item.transform.translation, item.transform.rotation
                transforms[key] = {
                    "parent": key[0],
                    "child": key[1],
                    "translation": [t.x, t.y, t.z],
                    "quaternion_xyzw": [q.x, q.y, q.z, q.w],
                }

    def receive_info(message):
        if message.header.frame_id != rgb:
            raise ValueError("CameraInfo frame does not match configured RGB frame")
        infos[:] = [
            {
                "frame": rgb,
                "R": list(message.r),
                "P": list(message.p),
                "width": message.width,
                "height": message.height,
            }
        ]

    rclpy.init()
    node = rclpy.create_node("board_accuracy_extrinsic_snapshot")
    try:
        node.create_subscription(
            TFMessage,
            "/tf_static",
            receive_tf,
            QoSProfile(
                depth=100,
                reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.TRANSIENT_LOCAL,
            ),
        )
        node.create_subscription(
            CameraInfo,
            camera["camera_info_topic"],
            receive_info,
            QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT),
        )
        deadline = time.monotonic() + 15
        while (
            required - transforms.keys() or not infos
        ) and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
        if required - transforms.keys() or not infos:
            raise ValueError(
                f"missing live TF {sorted(required - transforms.keys())} or RGB CameraInfo; nothing exported"
            )
        matrices = {
            key: matrix_from_transform(value["translation"], value["quaternion_xyzw"])
            for key, value in transforms.items()
        }
        raw_from_rectified = np.eye(4)
        raw_from_rectified[:3, :3] = np.asarray(infos[0]["R"]).reshape(3, 3).T
        rigid(raw_from_rectified)
        center_from_rgb = (
            np.linalg.inv(left_to_stereo_center(matrices[(left, right)]))
            @ matrices[(left, rgb)]
            @ raw_from_rectified
        )
        rigid(center_from_rgb)
        snapshot = {
            "captured_utc": datetime.now(timezone.utc).isoformat(),
            "ros_domain_id": int(os.environ["ROS_DOMAIN_ID"]),
            "source_topic": "/tf_static",
            "subscription_qos": "RELIABLE / TRANSIENT_LOCAL",
            "publishers": [
                item.node_name
                for item in node.get_publishers_info_by_topic("/tf_static")
            ],
            "transforms": list(transforms.values()),
            "rgb_camera_info": infos[0],
            "convention": "Insight9 mapper centre = left origin + half left-to-right translation; left orientation retained",
            "pose_body_frame_contract": camera["pose_body_frame"],
            "pose_from_rectified_image": center_from_rgb.tolist(),
        }
    finally:
        node.destroy_node()
        rclpy.shutdown()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
    with snapshot_path.open("x") as stream:
        stream.write(content)
    camera["pose_from_rectified_image"] = center_from_rgb.tolist()
    camera["extrinsic_source"] = {
        "snapshot": str(snapshot_path),
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
        "captured_utc": snapshot["captured_utc"],
    }
    with args.output.open("x") as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(
        json.dumps(
            {
                "config": str(args.output),
                "tf_snapshot": str(snapshot_path),
                "translation_mm": (center_from_rgb[:3, 3] * 1000).tolist(),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
