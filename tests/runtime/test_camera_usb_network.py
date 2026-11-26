"""Exercise the SDK's generated command, including its original missing mask."""
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml


@pytest.mark.parametrize("module_name", ["egohalo_rectifier", "looper_node"])
def test_mapping_processes_include_egohalo_relay_at_startup(tmp_path, monkeypatch, module_name):
    pytest.importorskip("rclpy")
    from importlib import import_module
    from insight_capture.runtime.ros import network

    class NetworkConfigured(Exception):
        pass

    def configure(config, *, include_relay_links=False):
        assert config["ros_domain_id"] == 20
        assert include_relay_links
        raise NetworkConfigured

    module = import_module(f"insight_capture.runtime.mapping.{module_name}")
    path = tmp_path / "cameras.json"
    path.write_text(json.dumps({"ros_domain_id": 20, "egohalo_rectification": {}}))
    monkeypatch.setattr(sys, "argv", [module_name, "--config", str(path)])
    monkeypatch.setenv("ROS_DOMAIN_ID", "20")
    monkeypatch.setattr(network, "configure_ros_network", configure)
    if module_name == "egohalo_rectifier":
        monkeypatch.setattr(module, "configure_ros_network", configure)
    with pytest.raises(NetworkConfigured):
        module.main()


@pytest.mark.parametrize("compose", ["docker-compose.yml", "deploy/docker-compose.yml"])
def test_ego_mapping_services_can_read_the_host_discovery_snapshot(compose):
    root = Path(__file__).resolve().parents[2]
    path = "/run/insight-capture-network"
    services = yaml.safe_load((root / compose).read_text())["services"]
    for name in ("insight-dashboard", "insight3-global-localizer", "egohalo-stereo-rectifier",
                 "device-domain-relay"):
        assert f"{path}:{path}:ro" in services[name]["volumes"], name

NETWORK = '[Match]\nName=usb0\n[Network]\nAddress=169.254.40.1/24\n'
BOOT = '#!/bin/bash\nifconfig usb0 169.254.40.1\nroute add default gw 169.254.40.2\n'
# These are the relevant two SDK statements, in their actual order.
GENERATOR = r'''#!/bin/bash
sed -i "s/Address=[0-9]\+\.[0-9]\+\.[0-9]\+\.[0-9]\+\(\/[0-9]\+\)\?/Address=$NEW_IP\/24/" "$DNS_SERVER_INIT_SCRIPT"
echo 'ifconfig usb0 '$NEW_IP'' >> "$IP_SCRIPT_FILE"
'''


@pytest.fixture
def usb_network():
    path = Path(__file__).resolve().parents[2] / 'deploy/camera-clock/usb_network.py'
    spec = importlib.util.spec_from_file_location('camera_usb_network', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def generate(tmp_path, source, address):
    boot = tmp_path / 'boot command.sh'
    network = tmp_path / 'network config'
    boot.unlink(missing_ok=True)
    network.write_text('Address=169.254.40.1/24\n')
    env = {**os.environ, 'NEW_IP': address, 'IP_SCRIPT_FILE': str(boot),
           'DNS_SERVER_INIT_SCRIPT': str(network)}
    subprocess.run(['bash', '-c', source], env=env, check=True)
    return shlex.split(boot.read_text())


def test_original_sdk_reproduces_missing_netmask(tmp_path):
    assert generate(tmp_path, GENERATOR, '169.254.50.1') == [
        'ifconfig', 'usb0', '169.254.50.1',
    ]


@pytest.mark.parametrize('prefix', [24, 20, 16])
@pytest.mark.parametrize('new_ip', ['169.254.50.1', '169.254.40.1', '10.21.77.1'])
def test_changed_ip_retains_declared_prefix(usb_network, tmp_path, prefix, new_ip):
    network = NETWORK.replace('/24', f'/{prefix}')
    generator = GENERATOR.replace('/24', f'/{prefix}')
    boot, generator, address = usb_network.repaired_scripts(network, BOOT, generator)
    mask = str(ipaddress.IPv4Network(f'0.0.0.0/{prefix}').netmask)
    assert generate(tmp_path, generator, new_ip) == ['ifconfig', 'usb0', new_ip, 'netmask', mask]
    assert f'ifconfig usb0 {address.ip} netmask {mask}' in boot
    assert boot.splitlines()[0] == BOOT.splitlines()[0]
    assert boot.splitlines()[-1] == BOOT.splitlines()[-1]
    assert usb_network.repaired_scripts(network, boot, generator)[:2] == (boot, generator)


def installation(tmp_path, usb_network, monkeypatch, generator=GENERATOR):
    directory = tmp_path / 'etc/init.d/looper'
    directory.mkdir(parents=True)
    network = tmp_path / 'etc/systemd/network/usb0.network'
    network.parent.mkdir(parents=True)
    network.write_text(NETWORK)
    boot = directory / 'ip_config.sh'
    target = directory / 'modify_ip.sh'
    boot.write_text(BOOT)
    target.write_text(generator)
    boot.chmod(0o751)
    target.chmod(0o700)
    run = subprocess.run
    monkeypatch.setattr(usb_network, 'subprocess', SimpleNamespace(run=lambda command, **kwargs:
        subprocess.CompletedProcess(command, 0) if command == ['sync'] else run(command, **kwargs)))
    return boot, target


def test_install_preserves_modes_and_does_not_rewrite_on_retry(usb_network, tmp_path, monkeypatch):
    boot, target = installation(tmp_path, usb_network, monkeypatch)
    result = usb_network.install(tmp_path)
    assert set(result['changed']) == {str(boot), str(target)}
    assert boot.stat().st_mode & 0o777 == 0o751
    assert target.stat().st_mode & 0o777 == 0o700
    before = [(p.stat().st_ino, p.stat().st_mtime_ns) for p in (boot, target)]
    assert usb_network.install(tmp_path)['changed'] == []
    assert before == [(p.stat().st_ino, p.stat().st_mtime_ns) for p in (boot, target)]


@pytest.mark.parametrize('source', [GENERATOR + '\nif [\n', GENERATOR.replace('ifconfig usb0', 'unsupported usb0')])
def test_failure_keeps_original_files_and_cleans_staging(usb_network, tmp_path, monkeypatch, source):
    boot, target = installation(tmp_path, usb_network, monkeypatch, source)
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        usb_network.install(tmp_path)
    assert boot.read_text() == BOOT
    assert target.read_text() == source
    assert sorted(p.name for p in boot.parent.iterdir()) == ['ip_config.sh', 'modify_ip.sh']


def test_mismatched_startup_address_is_not_rewritten(usb_network):
    with pytest.raises(ValueError, match='does not match'):
        usb_network.repaired_scripts(NETWORK, BOOT.replace('169.254.40.1', '169.254.50.1'), GENERATOR)


@pytest.mark.parametrize('include_relay_links, head_addresses', [
    (False, ['127.0.0.1']),
    (True, ['127.0.0.1', '192.0.2.2']),
], ids=['other-services', 'dashboard'])
def test_mixed_domain_camera_transport_follows_verified_usb_leases(
    monkeypatch, include_relay_links, head_addresses,
):
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        'camera_transport', root / 'insight_capture/runtime/ros/network.py')
    network = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(network)
    profile = root / 'config/devices/jetson-nx-egohalo/cameras.json'
    config = json.loads(profile.read_text())
    peers, configured = [], []
    monkeypatch.delenv('FASTRTPS_DEFAULT_PROFILES_FILE', raising=False)
    monkeypatch.delenv('FASTDDS_DEFAULT_PROFILES_FILE', raising=False)
    monkeypatch.setenv('RMW_IMPLEMENTATION', 'rmw_fastrtps_cpp')
    monkeypatch.setattr(network, '_dynamic_profile', None)
    monkeypatch.setattr(network, '_dynamic_excluded', frozenset())
    monkeypatch.setattr(network, '_dynamic_namespaces', None)
    monkeypatch.setattr(network, '_transport_library', SimpleNamespace(
        insight_set_capture_addresses=lambda value: configured.append(value.decode()) or 0))
    monkeypatch.setattr(network, 'resolved_cameras', lambda: peers.copy())
    monkeypatch.setattr(network, 'camera_links', set)

    peers.extend([
        dict(name='usb-head', index=11, address='192.0.2.2', namespace='egohalo'),
        dict(name='usb-extra', index=20, address='192.0.2.3', namespace='insight9_extra'),
    ])
    options = {'include_relay_links': True} if include_relay_links else {}
    path = network.configure_ros_network(config, **options)
    try:
        assert configured == [','.join(head_addresses)]

        peers.append(dict(name='usb-wrist', index=12, address='198.51.100.2', namespace='insight3_a'))
        network.refresh_ros_network(network.camera_transport_links())
        assert configured[-1] == ','.join([*head_addresses, '198.51.100.2'])

        peers.pop(0)
        peers[-1].update(name='usb-reconnected', index=13, address='203.0.113.2')
        network.refresh_ros_network(network.camera_transport_links())
        assert configured[-1] == '127.0.0.1,203.0.113.2'
        profile = path.read_text()
        assert '203.0.113.2' in profile
        assert '192.0.2.2' not in profile
        assert '192.0.2.3' not in profile
        assert '198.51.100.2' not in profile
    finally:
        path.unlink(missing_ok=True)
        monkeypatch.delenv('FASTRTPS_DEFAULT_PROFILES_FILE', raising=False)


@pytest.mark.parametrize("snapshot_error", [FileNotFoundError, ValueError])
def test_live_configured_relay_survives_missing_or_stale_discovery(monkeypatch, snapshot_error):
    from insight_capture.runtime.ros import network

    relay = network.CameraLink("usb-head", 11, "169.254.10.2")
    other = network.CameraLink("usb-other", 12, "169.254.20.2")
    links = {relay, other}
    peers = None
    updates = []

    def resolved():
        if peers is None:
            raise snapshot_error
        return peers

    monkeypatch.delenv("FASTRTPS_DEFAULT_PROFILES_FILE", raising=False)
    monkeypatch.delenv("FASTDDS_DEFAULT_PROFILES_FILE", raising=False)
    monkeypatch.setenv("RMW_IMPLEMENTATION", "rmw_fastrtps_cpp")
    for name in ("_dynamic_profile", "_dynamic_namespaces", "_dynamic_relay_addresses", "_dynamic_excluded"):
        monkeypatch.setattr(network, name, getattr(network, name))
    monkeypatch.setattr(network, "resolved_cameras", resolved)
    monkeypatch.setattr(network, "camera_links", lambda: links.copy())
    monkeypatch.setattr(network, "_transport_library", SimpleNamespace(
        insight_set_capture_addresses=lambda value: updates.append(value.decode()) or 0))
    config_path = Path(__file__).resolve().parents[2] / "config/devices/jetson-nx-egohalo/cameras.json"
    profile = network.configure_ros_network(json.loads(config_path.read_text()), include_relay_links=True)

    try:
        assert updates[-1] == f"127.0.0.1,{relay.address}"
        assert relay.address in profile.read_text()
        links.remove(relay)
        network.refresh_ros_network()
        assert updates[-1] == "127.0.0.1"
        links.add(relay)
        network.refresh_ros_network()
        assert updates[-1] == f"127.0.0.1,{relay.address}"

        peers = [{**vars(relay), "namespace": "insight9_extra"},
                 {**vars(other), "namespace": "egohalo"}]
        network.refresh_ros_network()
        assert updates[-1] == f"127.0.0.1,{other.address}"
    finally:
        profile.unlink(missing_ok=True)


@pytest.mark.parametrize("mixed", [False, True])
def test_release_migration_retires_only_the_old_relay_exclusion(tmp_path, mixed):
    from deploy.migrate_release import migrate

    templates = Path(__file__).resolve().parents[2] / "config"
    config = tmp_path / "config"
    config.mkdir()
    path = config / "cameras.json"
    cameras = json.loads((templates / "devices/jetson-nx-egohalo/cameras.json").read_text())
    excluded = ["169.254.10.2"]
    cameras["ros_transport"] = {"exclude_host_addresses": excluded}
    if mixed:
        excluded.append("192.0.2.1")
        cameras["ros_transport"]["camera_usb_only"] = True
    path.write_text(json.dumps(cameras))

    change = migrate(tmp_path, templates)

    current = json.loads(path.read_text())
    expected = {"camera_usb_only": True}
    if mixed:
        expected["exclude_host_addresses"] = ["192.0.2.1"]
    assert current["ros_transport"] == expected
    assert current["cameras"] == cameras["cameras"]
    assert json.loads((Path(change["backup"]) / "config/cameras.json").read_text()) == cameras
    assert "config/cameras.json" not in migrate(tmp_path, templates)["changed"]
