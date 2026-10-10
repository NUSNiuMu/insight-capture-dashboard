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

LEGACY_AVATAR_MODELS = {
    "iron-man_helmet_mk3_clean.glb": "iron-man_helmet_mk3_optimized.glb",
}

# Model defaults apply when cameras.json omits an explicit transform.
AVATAR_MODEL_DEFAULTS = {
    "MaleBaseModel_BravFG.glb": {
        "avatar_scale": 0.024,
        "avatar_rotation_deg_xyz": [-90.0, 180.0, 90.0],
    },
    "ArmBaseModel_BravFG.glb": {
        "avatar_scale": 0.015,
        "avatar_rotation_deg_xyz": [0.0, 0.0, 180.0],
    },
    "vis_assembly.glb": {
        "avatar_scale": 3.0,
        "avatar_rotation_deg_xyz": [0.0, 90.0, 0.0],
    },
    "iron-man_helmet_mk3_optimized.glb": {
        "avatar_scale": 0.5,
        "avatar_rotation_deg_xyz": [90.0, 0.0, -90.0],
    },
    "glove.glb": {
        "avatar_scale": 0.005,
        "avatar_rotation_deg_xyz": [-90.0, 0.0, -180.0],
    },
}


def avatar_model_defaults(avatar_model) -> Dict:
    if not avatar_model:
        return {}
    return AVATAR_MODEL_DEFAULTS.get(Path(avatar_model).name, {})


def canonical_avatar_model(avatar_model):
    if not avatar_model:
        return avatar_model
    path = Path(avatar_model)
    replacement = LEGACY_AVATAR_MODELS.get(path.name)
    return str(path.with_name(replacement)) if replacement else avatar_model


# Settings only offers models with tuned transforms.
AVAILABLE_AVATAR_MODELS = [
    {"file": "vis_assembly.glb", "label": "Vis Assembly (hand)"},
    {"file": "MaleBaseModel_BravFG.glb", "label": "Male Base Model"},
    {"file": "ArmBaseModel_BravFG.glb", "label": "Arm Base Model"},
    {"file": "iron-man_helmet_mk3_optimized.glb", "label": "Iron Man Helmet (head)"},
    {"file": "glove.glb", "label": "Glove (hand)"},
]


def load_setup(config_path: Path) -> Dict:
    config_path = Path(config_path)
    with config_path.open("r", encoding="utf-8") as f:
        config = json.load(f)

    for camera in config.get("cameras", []):
        camera["avatar_model"] = canonical_avatar_model(camera.get("avatar_model"))

    return config


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


def dashboard_streams(camera: Dict) -> List[Dict]:
    """Resolve image streams independently of the device's single pose."""
    streams = camera.get("dashboard_streams")
    if streams is None:
        stream = camera.get("dashboard_image_stream", "color_compressed")
        streams = [{
            "name": camera["name"],
            "topic": image_topic(camera["namespace"], stream),
            "camera_info_topic": camera_info_topic(camera["namespace"], stream),
            "type": IMAGE_STREAMS[stream]["type"],
        }]
    if not isinstance(streams, list) or not streams:
        raise ValueError(f"{camera['name']}: dashboard_streams must be a nonempty list")
    resolved = []
    for stream in streams:
        item = dict(stream)
        if not item.get("name") or not str(item.get("topic", "")).startswith("/"):
            raise ValueError("Every image stream needs a name and absolute ROS topic")
        if item.get("type") not in {"image", "compressed", "h264"}:
            raise ValueError(f"Unsupported image stream type: {item.get('type')}")
        if not str(item.get("camera_info_topic", "")).startswith("/"):
            raise ValueError("Every image stream needs an absolute camera_info_topic")
        item.update(namespace=camera["namespace"], device_name=camera["name"])
        item.setdefault("label", camera.get("dashboard_label", item["name"]))
        item.setdefault("rotation_deg", int(camera.get("dashboard_rotation_deg", 0)))
        item.setdefault("row", int(camera.get("dashboard_row", 0)))
        item.setdefault("column", int(camera.get("dashboard_column", 0)))
        item["hand_tracking"] = bool(camera.get("dashboard_hand_tracking", False))
        resolved.append(item)
    return resolved


def build_dashboard_config(config: Dict) -> Dict:
    dashboard = config.get("dashboard", {})
    cameras = []
    poses = []
    for camera in enabled_cameras(config):
        namespace = camera["namespace"]
        cameras.extend(dashboard_streams(camera))

        pose_stream = camera.get("dashboard_pose_stream", "vio_100hz")
        if pose_stream.startswith("vio_"):
            pose_rate = pose_stream.removeprefix("vio_")
            pose_topic = vio_topic(namespace, pose_rate)
        else:
            pose_topic = pose_stream
        avatar_model = camera.get("avatar_model")
        model_defaults = avatar_model_defaults(avatar_model)
        poses.append(
            {
                "name": camera["name"],
                "topic": pose_topic,
                "teleop_role": camera.get("teleop_role", camera["name"]),
                "avatar_model": avatar_model,
                "avatar_scale": float(camera.get("avatar_scale", model_defaults.get("avatar_scale", 1.0))),
                "avatar_rotation_deg_xyz": camera.get(
                    "avatar_rotation_deg_xyz", model_defaults.get("avatar_rotation_deg_xyz", [0.0, 0.0, 0.0])
                ),
                "avatar_offset_xyz": camera.get(
                    "avatar_offset_xyz", model_defaults.get("avatar_offset_xyz", [0.0, 0.0, 0.0])
                ),
            }
        )

    names = [camera["name"] for camera in cameras]
    topics = [camera["topic"] for camera in cameras]
    if len(set(names)) != len(names) or len(set(topics)) != len(topics):
        raise ValueError("Dashboard image stream names and topics must be unique")
    pose_names = [pose["name"] for pose in poses]
    if len(set(pose_names)) != len(pose_names):
        raise ValueError("Camera device names must be unique")
    return {
        "trajectory": dashboard.get("trajectory", {}),
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
