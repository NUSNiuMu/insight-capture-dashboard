"""Match a complete camera set and activate its configuration on an idle Jetson."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from insight_capture.runtime.discovery import camera_links, dhcp_peer, discover_camera, json_request


ROOT = Path(__file__).resolve().parents[2]
BINDINGS = "uvc-bindings.json"
ROLES = ("left", "right")
UQ2168_PRODUCT = "1bcf:28c4"
UQ2168_MODE = (1920, 1200, 90)
UQ2168_DATA = ("frame_timing", "imu_raw", "uvc_metadata")
HEAD = "egohalo"
BASE_EGO = "jetson-nx-egohalo"
PROFILES = {
    "jetson-nx": {"insight9_a", "insight3_a", "insight3_b"},
    BASE_EGO: {HEAD, "insight3_a", "insight3_b"},
    BASE_EGO + "-insight1": {HEAD, "insight1_a", "insight1_b"},
    BASE_EGO + "-uvc": {HEAD, "uvc_left", "uvc_right"},
}
NAMES = set.union(*PROFILES.values())
BUSY_STATES = {"preparing", "starting", "recording", "saving", "finalizing", "merging", "stopping"}


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def write_json(path, data):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            json.dump(data, output, indent=2, ensure_ascii=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), 0o644)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def uvc_devices(sysfs=Path("/sys/class/video4linux"), udev=Path("/run/udev/data")):
    """Select one image node per USB camera, excluding V4L2 metadata nodes."""
    devices = []
    for node in sorted(sysfs.glob("video*")):
        try:
            interface = (node / "device").resolve(strict=True)
            if (interface / "driver").resolve().name != "uvcvideo":
                continue
            usb = interface.parent
            major_minor = (node / "dev").read_text().strip()
            props = dict(line[2:].split("=", 1) for line in
                         (udev / ("c" + major_minor)).read_text().splitlines()
                         if line.startswith("E:") and "=" in line)
            if ":capture:" not in props.get("ID_V4L_CAPABILITIES", ""):
                continue
            product = ":".join((usb / name).read_text().strip() for name in ("idVendor", "idProduct"))
            serial_file = usb / "serial"
            serial = serial_file.read_text().strip() if serial_file.exists() else ""
            devices.append({"node": node.name, "port": usb.name, "serial": serial, "product": product,
                            "index": int((node / "index").read_text())})
        except (OSError, ValueError):
            continue  # The node may have disappeared or udev may still be initializing.
    return [device for device in devices if device["index"] ==
            min(d["index"] for d in devices if d["port"] == device["port"])]


def bound_uvc(devices, bindings):
    if not isinstance(bindings, dict) or set(bindings) != set(ROLES):
        return {}
    found = {}
    for role in ROLES:
        binding = bindings[role]
        field = "serial" if binding.get("serial") else "port"
        matches = [device for device in devices if device["product"] == binding["product"]
                   and device[field] == binding[field]]
        if len(matches) == 1:
            found[role] = matches[0]
    if len({device["port"] for device in found.values()}) != len(found):
        return {}
    return found


def bind_uvc(path, left, right):
    devices = uvc_devices()
    bindings = {}
    for role, port in zip(ROLES, (left, right)):
        matches = [device for device in devices if device["port"] == port]
        if len(matches) != 1:
            raise ValueError(f"Expected one capture device at USB port {port}")
        device = matches[0]
        serial = device["serial"]
        unique = serial and sum(d["product"] == device["product"] and d["serial"] == serial
                                for d in devices) == 1
        bindings[role] = {"product": device["product"], **({"serial": serial} if unique else {"port": port})}
    if len(bound_uvc(devices, bindings)) != 2:
        raise ValueError("Left and right must be different USB cameras")
    write_json(path, bindings)


def probe(link):
    interface = Path(f"/sys/class/net/{link.name}/device").resolve()
    port = interface.parent.name
    try:
        host = dhcp_peer(link)
    except (OSError, KeyError, ValueError, subprocess.SubprocessError):
        return None
    try:
        ego = json_request(f"http://{host}:8765/api/capture/participant", timeout=1)
        if ego.get("protocol") == 1 and ego.get("profile") == HEAD:
            return {**vars(link), "host": host, "namespace": HEAD, "port": port}
    except (OSError, ValueError, AttributeError):
        pass
    try:
        camera = discover_camera(link, host=host)
        if camera["namespace"] == HEAD:
            return None
        if camera["namespace"].startswith("insight1_"):
            model = json_request(f"http://{host}/api/ota/product-names", timeout=1)
            if model.get("success") is not True or "insight1" not in [
                    str(value).lower() for value in model.get("data", [])]:
                return None
        return {**camera, "port": port}
    except (OSError, KeyError, TypeError, AttributeError, ValueError, subprocess.SubprocessError):
        return None


def detect(network, usb):
    network_ports = {camera.get("port") for camera in network}
    usb = {role: device for role, device in usb.items() if device["port"] not in network_ports}
    names = Counter(camera["namespace"] for camera in network if camera["namespace"] in NAMES)
    names.update("uvc_" + role for role in usb)
    matches = [profile for profile, required in PROFILES.items()
               if all(names[name] == 1 for name in required)]
    if len(matches) != 1 or set(names) != PROFILES[matches[0]]:
        return None
    profile = matches[0]
    peers = {camera["namespace"]: {key: camera[key] for key in ("host", "address", "index")}
             for camera in network if camera["namespace"] in PROFILES[profile]}
    if len({p["host"] for p in peers.values()}) != len(peers):
        return None
    return {"profile": profile, "peers": peers,
            "usb": {role: {key: device[key] for key in ("product", "serial", "port")}
                    for role, device in usb.items()}}


def scan(config):
    with ThreadPoolExecutor() as pool:
        network = [camera for camera in pool.map(probe, camera_links()) if camera]
    devices = uvc_devices()
    wrists = bound_uvc(devices, read_json(config / BINDINGS, {}))
    automatic = False
    if len(wrists) != len(ROLES):
        uq2168 = sorted((device for device in devices if device["product"] == UQ2168_PRODUCT),
                        key=lambda device: device["port"])
        if len(uq2168) == len(ROLES) == len({device["port"] for device in uq2168}):
            wrists = dict(zip(ROLES, uq2168))
            automatic = True
    candidate = detect(network, wrists)
    if candidate and automatic:
        candidate["automatic_uvc"] = True
    return candidate


def mono_stream(namespace, topics):
    """Select an advertised Insight1 image stream and its exact ROS type."""
    prefix = f"/{namespace}/"
    options = []
    for name, types in topics.items():
        if not name.startswith(prefix):
            continue
        for suffix, rank in (
            ("/image_rect_raw", 0), ("/image_raw", 1), ("/image_rect_raw/compressed", 2),
            ("/image_raw/compressed", 3), ("/image", 4),
        ):
            if not name.endswith(suffix):
                continue
            kind = "compressed" if suffix.endswith("/compressed") else "image"
            expected = "sensor_msgs/msg/" + ("CompressedImage" if kind == "compressed" else "Image")
            if types == [expected]:
                options.append((rank, name, kind, suffix))
    if not options:
        return None
    _, topic, kind, suffix = min(options)
    info = topic.removesuffix(suffix) + "/camera_info"
    return {"name": namespace, "topic": topic, "type": kind,
            "camera_info_topic": info if topics.get(info) == ["sensor_msgs/msg/CameraInfo"] else ""}


def render(config, candidate, status):
    profile, peers = candidate["profile"], candidate["peers"]
    if profile == "jetson-nx":
        if (config / ".device").read_text().strip() == profile:
            current = read_json(config / "cameras.json", {})
            names = {camera.get("namespace") for camera in current.get("cameras", [])
                     if camera.get("enabled", True)}
            if names == PROFILES[profile]:
                return None
        return read_json(config / "devices" / profile / "cameras.json")

    active = (config / ".device").read_text().strip()
    cameras = read_json(config / "cameras.json") if active == profile else None
    is_uvc = profile.endswith("-uvc")
    uq2168 = is_uvc and all(candidate["usb"][role]["product"] == UQ2168_PRODUCT for role in ROLES)
    if is_uvc and cameras is not None:
        existing = bool(cameras["distributed_recording"].get("required_aux_topics"))
        if existing != uq2168:
            cameras = None
    if cameras is None:
        cameras = read_json(config / "devices" / BASE_EGO / "cameras.json")
        if profile != BASE_EGO:
            del cameras["recording_h264"]
            distributed = cameras["distributed_recording"]
            distributed["image_topics"] = [topic for topic in distributed["image_topics"]
                                           if topic.startswith("/egohalo/")]
            for index, (role, insight1_name) in enumerate(zip(("right", "left"),
                                                             ("insight1_a", "insight1_b"))):
                if profile.endswith("-uvc"):
                    name = "uvc_" + role
                    stream = {"name": name, "topic": f"/{name}/camera/image_raw/compressed",
                              "type": "compressed", "camera_info_topic": ""}
                else:
                    name = insight1_name
                    stream = mono_stream(name, status.get("camera_topics", {}))
                    if stream is None:
                        return None
                cameras["cameras"][index] = {
                    "name": name, "namespace": name, "teleop_role": role + "_hand",
                    "dashboard_row": index, "dashboard_label": name, "dashboard_pose_stream": "",
                    "dashboard_cov_stream": "", "dashboard_streams": [stream],
                    "local_uvc": profile.endswith("-uvc"),
                    "record_topics": [stream["camera_info_topic"]] if stream["camera_info_topic"] else [],
                }
                distributed["image_topics"].append(stream["topic"])
                distributed.setdefault("image_rates", {})[stream["topic"]] = 0
                if uq2168:
                    width, height, fps = UQ2168_MODE
                    stream.update(width=width, height=height)
                    camera = cameras["cameras"][index]
                    camera["uq2168"] = True
                    topics = [f"/{name}/camera/{leaf}" for leaf in UQ2168_DATA]
                    camera["record_topics"].extend(topics)
                    distributed.setdefault("required_aux_topics", []).extend(topics[:2])
                    distributed["image_rates"][stream["topic"]] = fps
    if uq2168:
        automatic = candidate.get("automatic_uvc", False)
        bindings = ({role: {key: device[key] for key in ("product", "port")}
                     for role, device in candidate["usb"].items()} if automatic
                    else read_json(config / BINDINGS, {}))
        labels = {"left": "腕部 1", "right": "腕部 2"} if automatic else {}
        for camera in cameras["cameras"]:
            if camera.get("uq2168"):
                role = camera["name"].removeprefix("uvc_")
                camera["uvc_binding"] = bindings.get(role, {})
                camera["dashboard_label"] = labels.get(role, camera["name"])
    head = next(camera for camera in cameras["cameras"] if camera["teleop_role"] == "head")
    head["relay_host_address"] = peers[HEAD]["address"]
    distributed = cameras["distributed_recording"]
    distributed["ego_url"] = f"http://{peers[HEAD]['host']}:8765"
    prior_devices = {device["name"].lower().replace(" ", "_"): device
                     for device in distributed["clock_devices"]}
    distributed["clock_devices"] = [
        {**prior_devices.get(name, {}), "name": name, "host": peers[name]["host"]}
        for name in sorted(peers)
    ]
    return cameras


def capture_status(port, topics=False):
    path = "/api/recording/status" + ("?camera_topics=1" if topics else "")
    return json_request(f"http://127.0.0.1:{port}{path}", timeout=5)


def idle(status):
    return (status.get("upgrade_lock_supported") is True
            and status.get("recording") is False
            and status.get("operation_in_progress") is False
            and status.get("merge_state") in {"idle", "saved", "done", "error"}
            and status.get("playback_state") != "playing"
            and (status.get("current_take") or {}).get("state") not in BUSY_STATES)


@contextmanager
def capture_lock(config):
    lock = config / ".capture-upgrade.lock"
    with os.fdopen(os.open(lock, os.O_CREAT | os.O_RDONLY | os.O_CLOEXEC, 0o644)) as descriptor:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield descriptor.fileno()


def pending_settings(config, candidate, port):
    active = (config / ".device").read_text().strip()
    profile = candidate["profile"]
    status = capture_status(port, topics=True) if profile.endswith("-insight1") and active != profile else {}
    cameras = render(config, candidate, status)
    if profile == active and cameras == read_json(config / "cameras.json"):
        return None
    return cameras


def activate(config, candidate, port):
    """Share the installer/recorder inode and recheck the set before stopping services."""
    if pending_settings(config, candidate, port) is None or not idle(capture_status(port)):
        return False
    try:
        with capture_lock(config) as descriptor:
            cameras = pending_settings(config, candidate, port)
            if cameras is None or not idle(capture_status(port)) or scan(config) != candidate:
                return False
            with tempfile.TemporaryDirectory(prefix=".camera-profile-", dir=config) as stage:
                settings = Path(stage) / "cameras.json"
                write_json(settings, cameras)
                subprocess.run([str(ROOT / "deploy/device_services.sh"), "switch", str(settings),
                                candidate["profile"]], check=True, cwd=ROOT, pass_fds=(descriptor,),
                               env={**os.environ, "DASHBOARD_PORT": str(port)})
    except BlockingIOError:
        return False
    return True


def watch(config, port):
    previous = None
    while True:
        delay = 2
        try:
            candidate = scan(config)
            if candidate and candidate == previous:
                if activate(config, candidate, port):
                    print("Selected camera profile:", candidate["profile"], flush=True)
            previous = candidate
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            print("Camera profile waiting:", exc, flush=True)
            previous = None
            delay = 15
        time.sleep(delay)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("scan")
    commands.add_parser("uvc-devices")
    bind = commands.add_parser("bind-uvc")
    bind.add_argument("--left", required=True)
    bind.add_argument("--right", required=True)
    commands.add_parser("watch")
    args = parser.parse_args()
    config = ROOT / "config"
    port = int(os.environ.get("DASHBOARD_PORT", 8765))
    try:
        if args.action == "watch":
            watch(config, port)
        elif args.action == "bind-uvc":
            with capture_lock(config):
                if ((config / ".device").is_file() and (config / ".device").read_text().strip().endswith("-uvc")
                        and not idle(capture_status(port))):
                    raise ValueError("Finish capture and save before rebinding UVC cameras")
                bind_uvc(config / BINDINGS, args.left, args.right)
        else:
            result = scan(config) if args.action == "scan" else uvc_devices()
            print(json.dumps(result, indent=2))
    except BlockingIOError:
        parser.exit(1, "Capture start or another configuration change is active\n")
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
