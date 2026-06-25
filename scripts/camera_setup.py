#!/usr/bin/env python3

import argparse
import json
from pathlib import Path
from typing import Dict, List


IMAGE_STREAMS = {
    "infra1": {"topic": "infra1/image_rect_raw", "type": "image"},
    "infra2": {"topic": "infra2/image_rect_raw", "type": "image"},
    "depth": {"topic": "depth/image_rect_raw", "type": "image"},
    "color": {"topic": "color/image_rect_raw", "type": "image"},
    "color_compressed": {"topic": "color/image_rect_raw/compressed", "type": "compressed"},
}


def load_setup(config_path: Path) -> Dict:
    with config_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def camera_base(namespace: str) -> str:
    return f"/{namespace}/camera"


def image_topic(namespace: str, stream: str) -> str:
    return f"{camera_base(namespace)}/{IMAGE_STREAMS[stream]['topic']}"


def camera_info_topic(namespace: str, stream: str) -> str:
    stream_name = "color" if stream.startswith("color") else stream
    return f"{camera_base(namespace)}/{stream_name}/camera_info"


def vio_topic(namespace: str, rate: str) -> str:
    return f"{camera_base(namespace)}/vio_{rate}"


def enabled_cameras(config: Dict) -> List[Dict]:
    return [camera for camera in config.get("cameras", []) if camera.get("enabled", True)]


def build_dashboard_config(config: Dict) -> Dict:
    dashboard = config.get("dashboard", {})
    cameras = []
    poses = []
    session_alignment = config.get("session_alignment", {})

    for camera in enabled_cameras(config):
        namespace = camera["namespace"]
        image_stream = camera["dashboard_image_stream"]
        cameras.append(
            {
                "name": camera["name"],
                "label": camera.get("dashboard_label", camera.get("label", camera["name"])),
                "topic": image_topic(namespace, image_stream),
                "camera_info_topic": camera_info_topic(namespace, image_stream),
                "type": IMAGE_STREAMS[image_stream]["type"],
                "rotation_deg": int(camera.get("dashboard_rotation_deg", 0)),
                "row": int(camera.get("dashboard_row", 0)),
                "column": int(camera.get("dashboard_column", 0)),
                "column_span": int(camera.get("dashboard_column_span", 1)),
                "row_span": int(camera.get("dashboard_row_span", 1)),
            }
        )

        pose_stream = camera.get("dashboard_pose_stream", "vio_100hz")
        if pose_stream.startswith("vio_"):
            pose_rate = pose_stream.removeprefix("vio_")
            pose_topic = vio_topic(namespace, pose_rate)
        else:
            pose_topic = pose_stream
        poses.append(
            {
                "name": camera["name"],
                "topic": pose_topic,
                "color": camera.get("dashboard_color", "#ffffff"),
                "teleop_role": camera.get("teleop_role", camera["name"]),
                "avatar_model": camera.get("avatar_model"),
                "avatar_scale": float(camera.get("avatar_scale", 1.0)),
                "avatar_rotation_deg_xyz": camera.get("avatar_rotation_deg_xyz", [0.0, 0.0, 0.0]),
            }
        )

    return {
        "window_title": dashboard.get("window_title", "Insight Monitoring Dashboard"),
        "trajectory": dashboard.get("trajectory", {}),
        "session_alignment": {
            "enabled": bool(session_alignment.get("enabled", False)),
            "alignment_frame": session_alignment.get("alignment_frame", "board_center"),
            "reference_camera": session_alignment.get("reference_camera"),
        },
        "cameras": cameras,
        "poses": poses,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--dashboard-json", action="store_true")
    parser.add_argument("--ros-domain-id", action="store_true")
    args = parser.parse_args()

    config = load_setup(Path(args.config))
    if args.dashboard_json:
        print(json.dumps(build_dashboard_config(config), ensure_ascii=False, indent=2))
        return
    if args.ros_domain_id:
        print(config.get("ros_domain_id", 10))
        return
    parser.error("Choose --dashboard-json or --ros-domain-id")


if __name__ == "__main__":
    main()
