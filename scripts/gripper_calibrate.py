#!/usr/bin/env python3

"""Interactive live calibration for gripper open/close tracking.

Subscribes to one camera's image stream, prints the live pixel distance
between the two ArUco finger markers, and on your cue captures the "fully
open" and "fully closed" readings. Writes both into
config/gripper_calibration.json for GripperTrackingMixin to consume.

Usage:
  python3 scripts/gripper_calibrate.py --camera insight7_a
"""

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage, Image as RosImage

sys.path.insert(0, str(Path(__file__).resolve().parent))
from camera_setup import build_dashboard_config, load_setup  # noqa: E402
from gripper_tracking import DEFAULT_CALIBRATION_PATH, GripperMarkerDetector  # noqa: E402


def decode_frame(topic_type: str, msg: object) -> "np.ndarray | None":
    if topic_type == "compressed":
        return cv2.imdecode(np.frombuffer(msg.data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if msg.width == 0 or msg.height == 0:
        return None
    data = np.frombuffer(msg.data, dtype=np.uint8)
    encoding = msg.encoding.lower()
    if encoding == "bgr8":
        return np.ascontiguousarray(data.reshape((msg.height, msg.width, 3)))
    if encoding == "rgb8":
        rgb = data.reshape((msg.height, msg.width, 3))
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    return None


DEBUG_FRAME_PATH = "/tmp/gripper_calibrate_debug.jpg"


class CalibrationNode(Node):
    def __init__(self, camera_name: str, topic: str, topic_type: str) -> None:
        super().__init__("gripper_calibrate")
        self.detector = GripperMarkerDetector()
        self.latest_distance_px = None
        self.latest_found_both = False
        self.frame_count = 0
        self.last_decode_failed = False
        self.last_image_shape = None
        self.last_ids_seen = []
        msg_type = CompressedImage if topic_type == "compressed" else RosImage
        self.create_subscription(msg_type, topic, self._on_image(topic_type), 5)
        self.get_logger().info(f"Subscribed to {camera_name}: {topic} ({topic_type})")

    def _on_image(self, topic_type: str):
        def callback(msg: object) -> None:
            image = decode_frame(topic_type, msg)
            if image is None:
                self.last_decode_failed = True
                return
            self.frame_count += 1
            self.last_decode_failed = False
            self.last_image_shape = image.shape
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            _, ids, _ = self.detector._detector.detectMarkers(gray)
            self.last_ids_seen = sorted(int(i) for i in ids.ravel()) if ids is not None else []
            result = self.detector.detect(image)
            self.latest_distance_px = result.distance_px
            self.latest_found_both = result.found_both
            if self.frame_count % 15 == 0:
                cv2.imwrite(DEBUG_FRAME_PATH, image)

        return callback


def wait_for_capture(node: CalibrationNode, prompt: str) -> float:
    print(prompt)
    captured = {}

    def reader() -> None:
        input()
        captured["go"] = True

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    while "go" not in captured:
        rclpy.spin_once(node, timeout_sec=0.05)
        if node.latest_found_both:
            status = f"distance = {node.latest_distance_px:7.1f} px"
        else:
            status = f"markers not both visible (seen this frame: {node.last_ids_seen or 'none'})"
        diag = f"frames={node.frame_count} shape={node.last_image_shape}"
        print(f"\r  live: {status}  [{diag}]  (press Enter to capture)   ", end="", flush=True)
    print()
    if not node.latest_found_both or node.latest_distance_px is None:
        raise RuntimeError("Both markers were not visible at capture time — reposition and retry.")
    return node.latest_distance_px


def main() -> None:
    project_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera", required=True, help="Camera name from config/cameras.json, e.g. insight7_a")
    parser.add_argument("--config", default=str(project_root / "config" / "cameras.json"))
    parser.add_argument("--calibration-out", default=DEFAULT_CALIBRATION_PATH)
    args = parser.parse_args()

    raw_config = load_setup(Path(args.config))
    dashboard_config = build_dashboard_config(raw_config)
    camera_entry = next((c for c in dashboard_config["cameras"] if c["name"] == args.camera), None)
    if camera_entry is None:
        raise SystemExit(f"Camera '{args.camera}' not found in {args.config}")

    # Must be set before rclpy.init() — DDS reads it at participant creation time.
    # Matches multi_camera_dashboard_web.py's own os.environ.setdefault call so this
    # standalone script joins the same DDS domain as the running dashboard/cameras.
    ros_domain_id = int(raw_config.get("ros_domain_id", 10))
    os.environ.setdefault("ROS_DOMAIN_ID", str(ros_domain_id))
    print(f"Using ROS_DOMAIN_ID={os.environ['ROS_DOMAIN_ID']}")

    rclpy.init()
    node = CalibrationNode(args.camera, camera_entry["topic"], camera_entry["type"])
    print(f"(debug: every 15th decoded frame is saved to {DEBUG_FRAME_PATH} for inspection)")
    try:
        open_px = wait_for_capture(node, "\nOpen the gripper all the way, then press Enter...")
        time.sleep(0.3)
        closed_px = wait_for_capture(node, "\nNow close the gripper all the way, then press Enter...")
    finally:
        node.destroy_node()
        rclpy.shutdown()

    print(f"\nCaptured: open={open_px:.1f}px  closed={closed_px:.1f}px")
    if abs(open_px - closed_px) < 5:
        print("WARNING: open/closed readings are nearly identical — calibration will be unreliable. Not saving.")
        raise SystemExit(1)

    calibration_path = Path(args.calibration_out)
    data = {}
    if calibration_path.is_file():
        try:
            data = json.loads(calibration_path.read_text())
        except (OSError, ValueError):
            data = {}
    data[args.camera] = {"open_px": open_px, "closed_px": closed_px}
    calibration_path.parent.mkdir(parents=True, exist_ok=True)
    calibration_path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"Saved to {calibration_path}")


if __name__ == "__main__":
    main()
