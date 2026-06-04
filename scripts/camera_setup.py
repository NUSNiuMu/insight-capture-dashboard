#!/usr/bin/env python3

import argparse
import json
from pathlib import Path
from typing import Dict, List


IMAGE_STREAMS = {
    "infra1": {"topic": "infra1/image_rect_raw", "type": "image", "record": True, "camera_info": True},
    "infra2": {"topic": "infra2/image_rect_raw", "type": "image", "record": True, "camera_info": True},
    "depth": {"topic": "depth/image_rect_raw", "type": "image", "record": True, "camera_info": False},
    "color": {"topic": "color/image_rect_raw", "type": "image", "record": True, "camera_info": True},
    "color_compressed": {"topic": "color/image_rect_raw/compressed", "type": "compressed", "record": True, "camera_info": True},
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


def simple_topic(namespace: str, suffix: str) -> str:
    return f"{camera_base(namespace)}/{suffix}"


def enabled_cameras(config: Dict) -> List[Dict]:
    return [camera for camera in config.get("cameras", []) if camera.get("enabled", True)]


def build_record_topics(config: Dict) -> List[str]:
    topics: List[str] = []
    for camera in enabled_cameras(config):
        namespace = camera["namespace"]
        for stream in camera.get("record_streams", []):
            if stream == "imu":
                topics.append(simple_topic(namespace, "imu"))
            elif stream == "vio_100hz":
                topics.append(vio_topic(namespace, "100hz"))
            elif stream == "vio_20hz":
                topics.append(vio_topic(namespace, "20hz"))
            elif stream == "vio_status":
                topics.append(simple_topic(namespace, "vio_status"))
            elif stream in IMAGE_STREAMS:
                if IMAGE_STREAMS[stream]["camera_info"]:
                    topics.append(camera_info_topic(namespace, stream))
                topics.append(image_topic(namespace, stream))

    if config.get("recording", {}).get("include_tf_static", True):
        topics.append("/tf_static")

    return topics


def build_path_entries(config: Dict) -> List[Dict]:
    entries: List[Dict] = []
    for camera in enabled_cameras(config):
        namespace = camera["namespace"]
        pose_stream = camera.get("path_pose_stream", "vio_20hz")
        if pose_stream.startswith("vio_"):
            pose_rate = pose_stream.removeprefix("vio_")
            pose_topic = vio_topic(namespace, pose_rate)
        else:
            pose_topic = pose_stream
        entries.append(
            {
                "name": camera["name"],
                "label": camera.get("label", camera["name"]),
                "pose_topic": pose_topic,
                "path_topic": camera.get("path_topic", f"/viz/{camera['name']}/path"),
            }
        )
    return entries


def build_dashboard_config(config: Dict) -> Dict:
    dashboard = config.get("dashboard", {})
    cameras = []
    poses = []

    for camera in enabled_cameras(config):
        namespace = camera["namespace"]
        image_stream = camera["dashboard_image_stream"]
        cameras.append(
            {
                "name": camera["name"],
                "label": camera.get("dashboard_label", camera.get("label", camera["name"])),
                "topic": image_topic(namespace, image_stream),
                "type": IMAGE_STREAMS[image_stream]["type"],
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
            }
        )

    return {
        "window_title": dashboard.get("window_title", "Insight Monitoring Dashboard"),
        "fullscreen": bool(dashboard.get("fullscreen", True)),
        "trajectory": dashboard.get("trajectory", {}),
        "cameras": cameras,
        "poses": poses,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--record-topics", action="store_true")
    parser.add_argument("--dashboard-json", action="store_true")
    parser.add_argument("--ros-domain-id", action="store_true")
    args = parser.parse_args()

    config = load_setup(Path(args.config))
    if args.record_topics:
        for topic in build_record_topics(config):
            print(topic)
        return
    if args.dashboard_json:
        print(json.dumps(build_dashboard_config(config), ensure_ascii=False, indent=2))
        return
    if args.ros_domain_id:
        print(config.get("ros_domain_id", 10))
        return
    parser.error("Choose one of --record-topics, --dashboard-json or --ros-domain-id")


if __name__ == "__main__":
    main()
