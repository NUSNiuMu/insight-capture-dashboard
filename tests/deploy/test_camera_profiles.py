import fcntl
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest

from insight_capture.runtime import camera_profiles as profiles
from insight_capture.core.config import build_dashboard_config
from insight_capture.media.preview_config import preview_description
from insight_capture.runtime.recording.recorder import build_default_topics
from insight_capture.runtime import uvc_bridge


def network(*names):
    return [{"namespace": name, "index": index, "port": f"2-{index}",
             "host": f"169.254.{index}.1", "address": f"169.254.{index}.2"}
            for index, name in enumerate(names, 1)]


def uvc(port, node, serial=""):
    return {"product": "1234:5678", "port": port, "serial": serial, "node": node}


@pytest.fixture
def config(tmp_path):
    shutil.copytree(profiles.ROOT / "config" / "devices", tmp_path / "devices")
    (tmp_path / ".device").write_text("jetson-nx\n")
    shutil.copyfile(tmp_path / "devices/jetson-nx/cameras.json", tmp_path / "cameras.json")
    return tmp_path


@pytest.mark.parametrize("profile", profiles.PROFILES)
def test_only_a_complete_unambiguous_set_can_match(profile):
    names = sorted(profiles.PROFILES[profile] - {"uvc_left", "uvc_right"})
    peers = network(*names)
    wrists = {"left": uvc("3-1", "video0"), "right": uvc("3-2", "video1")} if profile.endswith("-uvc") else {}
    assert profiles.detect(peers, wrists)["profile"] == profile
    assert profiles.detect(peers[:-1], wrists) is None
    assert profiles.detect(peers + [peers[0]], wrists) is None
    if wrists:
        assert profiles.detect(peers, {"left": wrists["left"]}) is None
        assert profiles.detect(peers, {**wrists, "left": uvc(peers[0]["port"], "video4")}) is None


def test_two_egohalo_combinations_connected_never_switch():
    peers = network("egohalo", "insight3_a", "insight3_b", "insight1_a", "insight1_b")
    assert profiles.detect(peers, {}) is None
    assert profiles.detect(peers[:3], {"left": uvc("3-1", "video0"), "right": uvc("3-2", "video1")}) is None


def test_binding_survives_replug_and_node_renumbering(tmp_path, monkeypatch):
    left, right = uvc("3-1", "video0", "L"), uvc("3-2", "video2")
    path = tmp_path / profiles.BINDINGS
    monkeypatch.setattr(profiles, "uvc_devices", lambda: [left, right])
    profiles.bind_uvc(path, "3-1", "3-2")
    binding = profiles.read_json(path)
    assert profiles.bound_uvc([right], binding) == {"right": right}
    left = uvc("3-3", "video12", "L")
    right = uvc("3-2", "video8")
    assert profiles.bound_uvc([right, left], binding) == {"left": left, "right": right}
    assert "left" not in profiles.bound_uvc([left, right, uvc("3-4", "video6", "L")], binding)


def test_insight1_uses_live_topics_and_egohalo_link(config):
    candidate = profiles.detect(network("egohalo", "insight1_a", "insight1_b"), {})
    graph = {
        "/insight1_a/camera/left/image_raw": ["sensor_msgs/msg/Image"],
        "/insight1_b/camera/left/image_raw/compressed": ["sensor_msgs/msg/CompressedImage"],
    }
    assert profiles.render(config, candidate, {}) is None
    settings = profiles.render(config, candidate, {"camera_topics": graph})
    head = settings["cameras"][-1]
    assert head["relay_host_address"] == candidate["peers"]["egohalo"]["address"]
    assert settings["distributed_recording"]["ego_url"] == "http://169.254.1.1:8765"
    assert set(graph).issubset(build_default_topics(settings))
    assert build_dashboard_config(settings)["cameras"][1]["type"] == "compressed"
    assert all("/insight3_" not in topic for topic in build_default_topics(settings))


def test_uvc_profile_records_jpeg_without_synthetic_vio(config):
    peers = network("egohalo")
    candidate = profiles.detect(peers, {"left": uvc("3-1", "video0"), "right": uvc("3-2", "video2")})
    settings = profiles.render(config, candidate, {})
    dashboard = build_dashboard_config(settings)
    wrist_poses = [pose for pose in dashboard["poses"] if pose["teleop_role"] != "head"]
    assert {pose["topic"] for pose in wrist_poses} == {
        "/insight_global/uvc_left/pose", "/insight_global/uvc_right/pose",
    }
    assert all(pose["visual_only"] for pose in wrist_poses)
    for side in ("left", "right"):
        root = f"/uvc_{side}/camera"
        topics = [topic for topic in build_default_topics(settings) if topic.startswith(root)]
        assert topics == [root + "/image_raw/compressed"]


def test_uq2168_preview_keeps_wrists_out_of_the_four_view_overview(config):
    wrists = {
        "left": dict(uvc("3-1", "video2"), product=profiles.UQ2168_PRODUCT),
        "right": dict(uvc("3-2", "video0"), product=profiles.UQ2168_PRODUCT),
    }
    candidate = profiles.detect(network("egohalo"), wrists)
    dashboard = build_dashboard_config(profiles.render(config, candidate, {}))
    preview = preview_description(
        [SimpleNamespace(**camera) for camera in dashboard["cameras"]],
        [SimpleNamespace(**pose) for pose in dashboard["poses"]],
    )
    overview = preview["overview"]
    assert preview["default_source"] == overview["id"] == "__overview__"
    assert overview["label"] == "四路总览"
    assert [region["id"] for region in overview["regions"]] == [
        "egohalo_side_left", "egohalo_stereo_left", "egohalo_stereo_right", "egohalo_side_right",
    ]
    assert all((region["width"], region["height"]) == (480, 640) for region in overview["regions"])
    assert [(source["id"], source["label"], source["width"], source["height"])
            for source in preview["sources"][-2:]] == [("uvc_left", "左腕", 1920, 1200), ("uvc_right", "右腕", 1920, 1200)]


def test_scan_selects_two_uq2168_without_a_binding_file(config, monkeypatch):
    head = network("egohalo")[0]
    first, second, third = (dict(uvc(port, node, "same-serial"), product=profiles.UQ2168_PRODUCT)
                            for port, node in [("1-2.3", "video2"), ("1-2.4", "video0"), ("1-2.5", "video8")])
    devices = [second, first, uvc("1-2.9", "video10")]
    monkeypatch.setattr(profiles, "camera_links", lambda: [head])
    monkeypatch.setattr(profiles, "probe", lambda link: link)
    monkeypatch.setattr(profiles, "uvc_devices", lambda: devices)

    candidate = profiles.scan(config)
    assert candidate["profile"] == "jetson-nx-egohalo-uvc"
    assert candidate["automatic_uvc"] is True
    assert [candidate["usb"][role]["port"] for role in profiles.ROLES] == ["1-2.3", "1-2.4"]
    devices.reverse()
    assert profiles.scan(config) == candidate
    settings = profiles.render(config, candidate, {})
    wrists = {camera["name"]: camera for camera in settings["cameras"] if camera.get("uq2168")}
    assert all("uvc_binding" not in camera for camera in wrists.values())
    labels = {camera["name"]: camera["label"] for camera in build_dashboard_config(settings)["cameras"]}
    assert (labels["uvc_left"], labels["uvc_right"]) == ("腕部 1", "腕部 2")
    assert not (config / profiles.BINDINGS).exists()

    for connected in ([first], [first, second, third]):
        devices[:] = connected
        assert profiles.scan(config) is None

    devices[:] = [first, second]
    profiles.bind_uvc(config / profiles.BINDINGS, second["port"], first["port"])
    explicit = profiles.scan(config)
    assert "automatic_uvc" not in explicit
    assert explicit["usb"]["left"]["port"] == second["port"]


def test_automatic_uq2168_moves_ports_without_a_profile_restart(config, monkeypatch):
    def candidate(left_port):
        wrists = {
            "left": dict(uvc(left_port, "video2"), product=profiles.UQ2168_PRODUCT),
            "right": dict(uvc("1-2.4", "video0"), product=profiles.UQ2168_PRODUCT),
        }
        return {**profiles.detect(network("egohalo"), wrists), "automatic_uvc": True}

    initial = candidate("1-2.3")
    settings = profiles.render(config, initial, {})
    # A deployment upgraded from the old automatic mode still stores port
    # hints. A new USB attachment only needs to change the running reader.
    for camera in settings["cameras"]:
        if camera.get("uq2168"):
            role = camera["name"].removeprefix("uvc_")
            camera["uvc_binding"] = {"product": profiles.UQ2168_PRODUCT, "port": initial["usb"][role]["port"]}
    profiles.write_json(config / "cameras.json", settings)
    (config / ".device").write_text("jetson-nx-egohalo-uvc\n")
    monkeypatch.setattr(profiles, "capture_status", lambda port: pytest.fail("USB move must not restart the profile"))
    assert not profiles.activate(config, candidate("4-1"), 8765)


def test_automatic_reader_keeps_each_take_on_its_original_attachment(config, monkeypatch):
    def camera(port, node, generation):
        return dict(uvc(port, node, "same-serial"), product=profiles.UQ2168_PRODUCT,
                    attachment=(port, generation, generation))

    first, other = camera("1-2.3", "video2", 7), camera("1-2.4", "video0", 8)
    replacement = camera(first["port"], first["node"], 9)
    moved = camera("4-1", "video8", 10)
    devices = [other, first]
    monkeypatch.setattr(uvc_bridge, "uvc_devices", lambda: devices)
    status = {"upgrade_lock_supported": True, "recording": False, "operation_in_progress": False,
              "merge_state": "idle"}
    monkeypatch.setattr(uvc_bridge, "capture_status", lambda port: status)
    sources = uvc_bridge.AutomaticUvcSources(config, 8765)
    assert sources.select("right") == other
    assert sources.select("left") == first

    devices[:] = [other, replacement]
    with (config / ".capture-upgrade.lock").open() as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        assert sources.select("left") is None  # Recording start admission owns this lock.

    status["recording"] = True
    assert sources.select("left") is None
    assert sources.select("right") == other
    assert uvc_bridge.AutomaticUvcSources(config, 8765).select("left") is None
    devices[:] = [other, moved]
    assert sources.select("left") is None

    status.update(recording=False, merge_state="finalizing")
    assert sources.select("left") is None
    status["merge_state"] = "saved"
    assert sources.select("left") == moved
    assert sources.select("right") == other

    devices[:] = [moved]
    assert sources.select("left") == moved
    assert sources.select("right") is None
    fresh = uvc_bridge.AutomaticUvcSources(config, 8765)
    assert fresh.select("right") is None
    assert fresh.select("left") == moved


def test_native_reader_rejects_a_reused_node_before_publishing(tmp_path, monkeypatch):
    usb = tmp_path / "1-2.3"
    interface = usb / "1-2.3:1.0"
    siblings = interface / "video4linux"
    (siblings / "video1").mkdir(parents=True)
    (siblings / "video1/index").write_text("1")
    (usb / "devnum").write_text("7")
    attachment = profiles.usb_attachment(usb)
    sysfs = tmp_path / "class"
    for name in ("video0", "video1"):
        (sysfs / name).mkdir(parents=True)
        (sysfs / name / "device").symlink_to(interface)
    native = uvc_bridge.uq2168
    monkeypatch.setattr(native, "Path", lambda _: sysfs)
    monkeypatch.setitem(sys.modules, "sensor_msgs.msg", SimpleNamespace(CompressedImage=object))
    monkeypatch.setitem(sys.modules, "std_msgs.msg", SimpleNamespace(UInt8MultiArray=object, UInt64MultiArray=object))
    process = SimpleNamespace(stdout=SimpleNamespace(fileno=lambda: 0, close=lambda: None),
                              terminate=lambda: None, wait=lambda **_: None)
    monkeypatch.setattr(native.subprocess, "Popen", lambda *args, **kwargs: process)

    def packets(_fd, _stop):
        (usb / "devnum").write_text("8")
        yield 1, 0, 0, b"jpeg"

    monkeypatch.setattr(native, "packets", packets)
    with pytest.raises(RuntimeError, match="UQ2168 changed while opening"):
        native.stream(Path("/host-dev/video0"), "left", {}, object(), attachment=attachment)


def test_camera_set_and_capture_state_are_checked_under_the_existing_lock(config, monkeypatch):
    candidate = profiles.detect(network("egohalo", "insight3_a", "insight3_b"), {})
    idle = {"upgrade_lock_supported": True, "recording": False, "operation_in_progress": False,
            "merge_state": "idle"}
    status = dict(idle, recording=True)
    attempts = []
    monkeypatch.setattr(profiles, "capture_status", lambda port: status)
    monkeypatch.setattr(profiles.subprocess, "run", lambda *a, **kw: attempts.append(a))
    monkeypatch.setattr(profiles, "scan", lambda config: candidate)
    assert not profiles.activate(config, candidate, 8765)
    assert not attempts

    status = dict(idle, current_take={"state": "saving"})
    assert not profiles.activate(config, candidate, 8765)
    status = idle
    with (config / ".capture-upgrade.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        assert not profiles.activate(config, candidate, 8765)

    monkeypatch.setattr(profiles, "scan", lambda config: None)
    assert not profiles.activate(config, candidate, 8765)
    assert not attempts
    monkeypatch.setattr(profiles, "scan", lambda config: candidate)

    def check_exclusive(args, **kwargs):
        with (config / ".capture-upgrade.lock").open() as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        settings = profiles.read_json(Path(args[2]))
        assert settings["distributed_recording"]["ego_url"] == "http://169.254.1.1:8765"
        attempts.append(args)

    monkeypatch.setattr(profiles.subprocess, "run", check_exclusive)
    assert profiles.activate(config, candidate, 8765)
    assert len(attempts) == 1
    assert attempts[0][1] == "switch"


def test_same_profile_replug_does_not_take_capture_lock(config, monkeypatch):
    candidate = profiles.detect(network("egohalo", "insight3_a", "insight3_b"), {})
    profiles.write_json(config / "cameras.json", profiles.render(config, candidate, {}))
    (config / ".device").write_text("jetson-nx-egohalo\n")
    monkeypatch.setattr(profiles, "capture_status", lambda port: pytest.fail("No restart for an unchanged profile"))
    assert not profiles.activate(config, candidate, 8765)


def test_uvc_reader_reopens_new_video_node_on_same_ros_topic(monkeypatch):
    left, replugged = uvc("3-1", "video0", "L"), uvc("3-2", "video8", "L")
    binding = {"left": {"serial": "L", "product": left["product"]},
               "right": {"port": "3-4", "product": left["product"]}}
    scans = iter([[left], [], [replugged]])
    monkeypatch.setattr(uvc_bridge, "read_json", lambda *args: binding)
    monkeypatch.setattr(uvc_bridge, "uvc_devices", lambda: next(scans))
    active = True
    opened, closed, frames = [], [], []

    class Pipeline:
        def __init__(self, spec):
            opened.append(spec.split()[1])
            self.delivered = False

        def set_state(self, state):
            if state == "null":
                closed.append(state)

        def get_by_name(self, name):
            return self

        def get_clock(self):
            return None

        def emit(self, *args):
            if self.delivered:
                return None
            self.delivered = True
            buffer = SimpleNamespace(pts=0, get_size=lambda: 4,
                                     extract_dup=lambda *args: b"jpeg")
            return SimpleNamespace(get_buffer=lambda: buffer)

    def publish(message):
        nonlocal active
        frames.append(message)
        active = len(frames) < 2

    gst = SimpleNamespace(parse_launch=Pipeline, State=SimpleNamespace(PLAYING="playing", NULL="null"),
                          StateChangeReturn=SimpleNamespace(FAILURE="failure"), SECOND=10**9,
                          CLOCK_TIME_NONE=-1)
    stop = SimpleNamespace(is_set=lambda: not active, wait=lambda delay: None)
    def image():
        return SimpleNamespace(header=SimpleNamespace(stamp=SimpleNamespace()))

    uvc_bridge.run_camera(None, "left", SimpleNamespace(publish=publish), stop, gst, image, RuntimeError)
    assert opened == ["device=/host-dev/video0", "device=/host-dev/video8"]
    assert len(closed) == 2
    assert all(frame.header.frame_id == "uvc_left_optical" for frame in frames)


@pytest.mark.parametrize("fail_start", [False, True])
def test_service_switch_publishes_marker_only_after_restart(config, tmp_path_factory, monkeypatch, fail_start):
    import os
    import subprocess

    root = tmp_path_factory.mktemp("installation")
    shutil.copytree(config, root / "config")
    (root / "deploy").mkdir()
    helper = root / "deploy/device_services.sh"
    shutil.copy2(profiles.ROOT / "deploy/device_services.sh", helper)
    old_settings = (root / "config/cameras.json").read_bytes()
    candidate = profiles.detect(network("egohalo"), {"left": uvc("3-1", "video0"), "right": uvc("3-2", "video2")})
    settings = root / "next.json"
    new = profiles.render(config, candidate, {})
    profiles.write_json(settings, new)
    bin_dir = root / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text("""#!/bin/bash
if [[ "$*" == "compose up -d "* ]]; then
    echo "$*" >> compose.log
    if [[ "${FAIL_TEST_UVC:-}" == 1 && "$*" == *uvc-wrist-inputs* ]]; then
        exit 44
    fi
fi
""")
    curl = bin_dir / "curl"
    curl.write_text("#!/bin/bash\nexit 0\n")
    for script in (docker, curl):
        script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ["PATH"])
    monkeypatch.setenv("FAIL_TEST_UVC", "1" if fail_start else "0")
    result = subprocess.run([str(helper), "switch", str(settings), candidate["profile"]],
                            capture_output=True, text=True, timeout=10)
    marker = (root / "config/.device").read_text().strip()
    live = root / "config/cameras.json"
    attempts = (root / "compose.log").read_text().splitlines()
    assert "uvc-wrist-inputs" in attempts[0]
    if fail_start:
        assert result.returncode != 0
        assert marker == "jetson-nx"
        assert live.read_bytes() == old_settings
        assert "uvc-wrist-inputs" not in attempts[-1]
    else:
        assert result.returncode == 0
        assert marker == candidate["profile"]
        assert profiles.read_json(live) == new
