"""Recording topic normalization, grouping, and live discovery."""

import os
import subprocess
from typing import Callable, Dict, List, Optional, Sequence, Set

from camera_setup import camera_base, camera_info_topic, enabled_cameras, image_topic

def _normalize_topic_name(topic: str) -> str:
    value = str(topic or "").strip()
    if not value:
        return ""
    if not value.startswith("/"):
        return f"/{value}"
    return value


def _normalize_topics(topics: Sequence[str]) -> List[str]:
    ordered: List[str] = []
    seen: Set[str] = set()
    for topic in topics:
        normalized = _normalize_topic_name(topic)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _camera_pose_topic(namespace: str, pose_stream: str) -> str:
    value = str(pose_stream or "").strip()
    if not value:
        return ""
    if value.startswith("/"):
        return value
    return f"{camera_base(namespace)}/{value}"


def build_default_topics(raw_config: Dict) -> List[str]:
    topics: List[str] = ["/tf_static"]
    for camera in enabled_cameras(raw_config):
        namespace = str(camera["namespace"])
        pose_topic = _camera_pose_topic(namespace, str(camera.get("dashboard_pose_stream", "vio_100hz")))
        if pose_topic:
            topics.append(pose_topic)
        # Keep the native VIO stream even when the dashboard renders a mapped
        # global pose. UMI single-arm export requires this local trajectory.
        topics.append(f"{camera_base(namespace)}/vio_100hz")

        image_stream = str(camera.get("dashboard_image_stream", "color_compressed"))
        topics.append(f"{camera_base(namespace)}/imu")
        topics.append(camera_info_topic(namespace, image_stream))
        topics.append(image_topic(namespace, image_stream))
        cov_stream = str(camera.get("dashboard_cov_stream", "vio_image_cov"))
        topics.append(f"{camera_base(namespace)}/{cov_stream}")
        # HandEngine streams remain available for explicit selection in the
        # Recording page, but are optional analysis data and not default.
    return _normalize_topics(topics)


def filter_recordable_live_topics(raw_config: Dict, live_topics: Sequence[str]) -> List[str]:
    enabled = {
        str(camera["namespace"]): camera
        for camera in enabled_cameras(raw_config)
    }
    filtered: List[str] = []
    for topic in live_topics:
        normalized = _normalize_topic_name(topic)
        if normalized == "/tf_static":
            filtered.append(normalized)
            continue
        if normalized.startswith(("/insight9_sparse_map/", "/insight_global/")):
            filtered.append(normalized)
            continue
        for namespace in enabled:
            prefix = f"/{namespace}/camera/"
            if normalized.startswith(prefix):
                filtered.append(normalized)
                break
    return _normalize_topics(filtered)


def build_recording_topic_catalog(
    raw_config: Dict,
    topics: Sequence[str],
    default_selected_topics: Optional[Sequence[str]] = None,
) -> Dict[str, object]:
    normalized_topics = _normalize_topics(topics)
    default_selected = set(_normalize_topics(default_selected_topics or []))
    topics_by_camera: Dict[str, List[Dict[str, str]]] = {}
    enabled = enabled_cameras(raw_config)
    cameras: List[Dict[str, object]] = []
    other: List[Dict[str, str]] = []

    for camera in enabled:
        namespace = str(camera["namespace"])
        topics_by_camera[namespace] = []

    for topic in normalized_topics:
        if topic == "/tf_static":
            other.append(
                {
                    "name": topic,
                    "short_name": "Other - tf_static",
                    "group": "Other",
                }
            )
            continue

        matched = False
        for camera in enabled:
            namespace = str(camera["namespace"])
            prefix = f"/{namespace}/camera/"
            global_prefix = (
                "/insight9_sparse_map/"
                if namespace == "insight9_a"
                else f"/insight_global/{namespace}/"
            )
            if topic.startswith(prefix):
                tail = topic[len(prefix) :]
            elif topic.startswith(global_prefix):
                tail = f"global/{topic[len(global_prefix):]}"
            else:
                continue
            topics_by_camera[namespace].append(
                {
                    "name": topic,
                    "short_name": f"{namespace} - {tail}",
                    "group": namespace,
                }
            )
            matched = True
            break
        if not matched:
            tail = topic.lstrip("/")
            other.append(
                {
                    "name": topic,
                    "short_name": f"Other - {tail}",
                    "group": "Other",
                }
            )

    for camera in enabled:
        namespace = str(camera["namespace"])
        entries = sorted(topics_by_camera[namespace], key=lambda item: item["short_name"])
        cameras.append(
            {
                "namespace": namespace,
                "label": camera.get("dashboard_label", camera["name"]),
                "topics": entries,
            }
        )

    other = sorted(other, key=lambda item: item["short_name"])
    return {
        "cameras": cameras,
        "other": other,
        "topics": normalized_topics,
        "default_selected_topics": [topic for topic in normalized_topics if topic in default_selected],
    }


def _parse_topic_list_with_types(output: str) -> List[str]:
    topics: List[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if " [" in line:
            name = line.split(" [", 1)[0].strip()
        else:
            name = line
        if name:
            topics.append(name)
    return topics


def discover_live_topics(
    raw_config: Dict,
    ros_domain_id: int,
    publisher_checker: Optional[Callable[[str], bool]] = None,
) -> Dict[str, object]:
    env = os.environ.copy()
    env["ROS_DOMAIN_ID"] = str(int(ros_domain_id))
    try:
        result = subprocess.run(
            ["ros2", "topic", "list", "-t"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5.0,
            env=env,
            check=False,
        )
    except Exception:
        return build_recording_topic_catalog(raw_config, [], build_default_topics(raw_config))

    discovered = _parse_topic_list_with_types(result.stdout or "")
    filtered = filter_recordable_live_topics(raw_config, discovered)
    if publisher_checker is not None:
        with_publishers = []
        for topic in filtered:
            if topic == "/tf_static":
                with_publishers.append(topic)
                continue
            try:
                if publisher_checker(topic):
                    with_publishers.append(topic)
            except Exception:
                continue
        filtered = with_publishers
    return build_recording_topic_catalog(raw_config, filtered, build_default_topics(raw_config))


def _topic_group(topic: str) -> str:
    """Keep global poses with their camera recorder instead of spawning extras."""

    if topic.startswith("/insight9_sparse_map/"):
        return "insight9_a"
    for namespace in ("insight3_a", "insight3_b"):
        if topic.startswith(f"/insight_global/{namespace}/"):
            return namespace
    return topic.strip("/").split("/", 1)[0]
