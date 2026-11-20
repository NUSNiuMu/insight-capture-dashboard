"""Exercise owned host files and service ordering without touching the real host."""

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("network_install", ROOT / "deploy/install_network_services.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
CHRONY = "chrony.service"
DISCOVERY = "insight-capture-camera-discovery.service"
AP = "insight-capture-hotspot.service"
DHCP = "insight-capture-dhcp.service"
ISOLATION = "insight-capture-isolation.service"


class Services:
    def __init__(self):
        self.calls = []
        self.active = {CHRONY}
        self.enabled = {CHRONY}
        self.profile_exists = True
        self.invalid_dns = False

    def __call__(self, args, *, check=True):
        self.calls.append(args)
        code, output = 0, ""
        if args[0] == "nmcli" and not self.profile_exists:
            code = 10
        elif args[0] == "dnsmasq" and self.invalid_dns:
            code = 1
        elif args[0] == "systemctl":
            action, service = args[1], args[-1]
            if action == "is-enabled":
                output = "enabled\n" if service in self.enabled else "disabled\n"
                code = int(service not in self.enabled)
            elif action == "is-active":
                code = 0 if service in self.active else 3
            elif action == "enable":
                self.enabled.add(service)
            elif action == "disable":
                self.enabled.discard(service)
            elif action in {"start", "restart", "reload"}:
                if service == CHRONY and action == "restart":
                    self.active.discard(DISCOVERY)
                if service == ISOLATION and action == "restart":
                    self.active.discard(AP)
                self.active.add(service)
            elif action != "daemon-reload":
                raise AssertionError(args)
        if check and code:
            raise subprocess.CalledProcessError(code, args)
        return subprocess.CompletedProcess(args, code, stdout=output, stderr="")


@pytest.fixture
def host(tmp_path):
    root = tmp_path / "host"
    for path, data in {
        "etc/insight-capture-hotspot.conf": "UPLINK=wlan-site\nAP_CHANNEL=149\n",
        "etc/NetworkManager/system-connections/jetson.nmconnection": "site-owned SSID and fixture password\n",
        "etc/modprobe.d/insight-capture-hotspot.conf": "site-owned driver options\n",
        "lib/modules/fixture/rtl8822ce.ko": "site-owned driver binary\n",
        "etc/chrony/chrony.conf": "pool old-source.example.invalid\n",
        "run/insight-capture-network/startup.json": '{"existing-camera": {"action": "already_ready"}}\n',
        "var/lib/misc/insight-capture.leases": "preserved DHCP lease\n",
    }.items():
        item = root / path
        item.parent.mkdir(parents=True, exist_ok=True)
        item.write_text(data)
    return root, Services()


def apply(host, **kwargs):
    root, services = host
    return installer.install(ROOT / "deploy", root, run=services, which=lambda name: "/bin/" + name, **kwargs)


def file_snapshot(root):
    return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
            for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("station_connected", [True, False])
def test_hotspot_disables_radio_power_save_without_reconnecting(tmp_path, station_connected):
    tools = tmp_path / "bin"
    tools.mkdir()
    calls = tmp_path / "calls"
    shim = tools / "shim"
    shim.write_text('''#!/usr/bin/env bash
name=${0##*/}
printf '%s %s\\n' "$name" "$*" >> "$CALLS"
case "$name $*" in
    'iw dev wlan-site link')
        if [[ $STATION_CONNECTED == yes ]]; then echo 'Connected to fixture'; fi ;;
    'iw dev capture_ap info'|'iw dev wlan-site info') echo 'channel 149' ;;
    'iw dev capture_ap get power_save'|'iw dev wlan-site get power_save') echo 'Power save: on' ;;
    'nmcli -g GENERAL.CONNECTION device show capture_ap') echo jetson-capture-hotspot ;;
    'sleep 5') exit 91 ;;
esac
''')
    shim.chmod(0o755)
    for command in ("iw", "ip", "nmcli", "sleep"):
        (tools / command).symlink_to(shim)
    script = (ROOT / "deploy/hotspot/start.sh").read_text()
    script = script.replace("source /etc/insight-capture-hotspot.conf", "UPLINK=wlan-site", 1)
    result = subprocess.run(["bash", "-c", script], timeout=5,
                            env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}",
                                 "CALLS": str(calls), "STATION_CONNECTED": "yes" if station_connected else "no"})
    assert result.returncode == 91
    operations = calls.read_text().splitlines()
    assert operations.count("iw dev capture_ap set power_save off") == 1
    assert ("iw dev wlan-site set power_save off" in operations) is station_connected
    assert not any(line.startswith(("nmcli connection modify", "nmcli --wait")) for line in operations)


def test_existing_hotspot_gets_all_owned_files_and_keeps_site_settings(host):
    root, services = host
    before = file_snapshot(root)
    result = apply(host)
    assert result["hotspot"]
    assert len(result["changed"]) == 9
    assert services.active == {CHRONY, DISCOVERY, AP, DHCP, ISOLATION}
    assert services.enabled == services.active
    after = file_snapshot(root)
    for name, content in before.items():
        if name != "etc/chrony/chrony.conf":
            assert after[name] == content
    assert len(list((root / "etc/chrony").glob("chrony.conf.before-insight-*"))) == 1
    assert not any(call[0] == "nmcli" and "modify" in call for call in services.calls)


def test_repeat_install_neither_rewrites_nor_restarts(host):
    root, services = host
    apply(host)
    before = file_snapshot(root)
    services.calls.clear()
    result = apply(host)
    for key in ("changed", "enabled", "started", "restarted", "reloaded"):
        assert result[key] == []
    assert file_snapshot(root) == before
    assert not any(call[:2] == ["systemctl", "daemon-reload"] for call in services.calls)


@pytest.mark.parametrize("path,service", [
    ("usr/local/libexec/insight-capture-hotspot", AP),
    ("usr/local/libexec/insight-capture-camera-discovery", DISCOVERY),
    ("etc/insight-capture-dnsmasq.conf", DHCP),
])
def test_changed_component_restarts_only_its_service(host, path, service):
    root, services = host
    apply(host)
    (root / path).write_bytes((root / path).read_bytes() + b"\n# old installed revision\n")
    result = apply(host)
    assert result["changed"] == ["/" + path]
    assert result["restarted"] == [service]
    assert result["started"] == result["reloaded"] == []


def test_isolation_update_reloads_without_stopping_required_hotspot(host):
    root, services = host
    apply(host)
    unit = root / "etc/systemd/system" / ISOLATION
    unit.write_text(unit.read_text().replace("ExecReload=/usr/local/libexec/insight-capture-isolate\n", ""))
    services.calls.clear()
    result = apply(host)
    assert result["reloaded"] == [ISOLATION]
    assert result["restarted"] == result["started"] == []
    assert AP in services.active
    assert services.calls.index(["systemctl", "daemon-reload"]) < services.calls.index(["systemctl", "reload", ISOLATION])


def test_chrony_update_restores_dependent_discovery(host):
    root, services = host
    apply(host)
    (root / "etc/chrony/chrony.conf").write_text("pool stale.example.invalid\n")
    result = apply(host)
    assert result["restarted"] == [CHRONY]
    assert result["started"] == [DISCOVERY]
    assert DISCOVERY in services.active


def test_inactive_service_is_started_without_rewriting_files(host):
    root, services = host
    apply(host)
    before = file_snapshot(root)
    services.active.remove(DHCP)
    result = apply(host)
    assert result["started"] == [DHCP]
    assert result["changed"] == result["restarted"] == []
    assert file_snapshot(root) == before


def test_first_driver_setup_defers_all_service_start_actions(host):
    root, services = host
    result = apply(host, hotspot=True, defer_start=True)
    assert result["changed"] and result["enabled"]
    assert result["started"] == result["restarted"] == result["reloaded"] == []
    assert services.active == {CHRONY}


@pytest.mark.parametrize("failure", ["missing_tool", "invalid_dns", "missing_profile", "missing_source"])
def test_validation_failure_changes_no_host_files_or_services(host, tmp_path, failure):
    root, services = host
    before = file_snapshot(root)
    which = lambda name: None if failure == "missing_tool" and name == "dnsmasq" else "/bin/" + name
    services.invalid_dns = failure == "invalid_dns"
    services.profile_exists = failure != "missing_profile"
    source = ROOT / "deploy"
    if failure == "missing_source":
        source = tmp_path / "source/deploy"
        shutil.copytree(ROOT / "deploy", source)
        (source / "hotspot/insight-capture-dhcp.service").unlink()
        (source.parent / "insight_capture/runtime").mkdir(parents=True)
        shutil.copy2(ROOT / "insight_capture/runtime/discovery.py", source.parent / "insight_capture/runtime")
    with pytest.raises((OSError, RuntimeError, subprocess.SubprocessError)):
        installer.install(source, root, run=services, which=which)
    assert file_snapshot(root) == before
    assert not any(call[0] == "systemctl" for call in services.calls)


def test_unconfigured_host_installs_discovery_without_inventing_wifi_profile(host):
    root, services = host
    (root / "etc/insight-capture-hotspot.conf").unlink()
    services.profile_exists = False
    result = apply(host)
    assert not result["hotspot"]
    assert services.active == {CHRONY, DISCOVERY}
    assert not (root / "usr/local/libexec/insight-capture-hotspot").exists()


def test_explicit_hotspot_requires_initial_site_configuration(host):
    root, services = host
    (root / "etc/insight-capture-hotspot.conf").unlink()
    before = file_snapshot(root)
    with pytest.raises(RuntimeError, match="UPLINK"):
        apply(host, hotspot=True)
    assert file_snapshot(root) == before


def test_owned_symlink_is_replaced_without_modifying_its_target(host):
    root, services = host
    apply(host)
    external = root / "site-original-script"
    external.write_text("keep this external file unchanged\n")
    path = root / "usr/local/libexec/insight-capture-hotspot"
    path.unlink()
    path.symlink_to(external)
    result = apply(host)
    assert not path.is_symlink()
    assert external.read_text() == "keep this external file unchanged\n"
    assert result["restarted"] == [AP]


def test_obsolete_camera_os_reboot_chain_is_disabled_without_stopping_it(host):
    root, services = host
    apply(host)
    unit = root / "etc/systemd/system/insight-camera-reboot.service"
    unit.write_text("[Service]\nExecStart=/old/reboot\n")
    services.calls.clear()
    apply(host)
    assert not unit.exists()
    assert ["systemctl", "disable", "insight-camera-reboot.service"] in services.calls
    assert not any("stop" in call or "--now" in call for call in services.calls)
