"""Identify camera network generations independently of device IP assignments."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.request import ProxyHandler, build_opener

SNAPSHOT = Path('/run/insight-capture-network/cameras.json')

@dataclass(frozen=True)
class CameraLink:
    name: str
    index: int
    address: str


def camera_links() -> set[CameraLink]:
    """Discover up, multicast-capable CDC-NCM links without fixed device IPs."""
    links = set()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        for index, name in socket.if_nameindex():
            try:
                driver = Path(f"/sys/class/net/{name}/device/driver").resolve().name
                if driver != "cdc_ncm":
                    continue
                request = struct.pack("256s", name.encode()[:15])
                flags = struct.unpack_from(
                    "H", fcntl.ioctl(control.fileno(), 0x8913, request), 16
                )[0]  # SIOCGIFFLAGS
                if flags & 0x1001 != 0x1001:  # IFF_UP | IFF_MULTICAST
                    continue
                packed = fcntl.ioctl(control.fileno(), 0x8915, request)  # SIOCGIFADDR
                address = socket.inet_ntoa(packed[20:24])
                if socket.if_nametoindex(name) == index:
                    links.add(CameraLink(name, index, address))
            except OSError:
                # udev may rename/remove a link or assign its address mid-scan.
                continue
    return links


def discover_camera(link):
    """Use the USB DHCP server and camera API, never a guessed .1 endpoint."""
    result = subprocess.run(
        ['nmcli', '-t', '-f', 'DHCP4.OPTION', 'device', 'show', link.name],
        capture_output=True, text=True, check=True, timeout=3)
    options = dict(line.split(':', 1)[-1].split(' = ', 1)
                   for line in result.stdout.splitlines() if ' = ' in line)
    host = str(ipaddress.IPv4Address(options['dhcp_server_identifier']))
    network = ipaddress.IPv4Network(f"{link.address}/{options['subnet_mask']}", strict=False)
    if ipaddress.IPv4Address(host) not in network or host == link.address:
        raise ValueError('Camera DHCP server is not on its USB link')
    opener = build_opener(ProxyHandler({}))
    def get(path):
        with opener.open(f'http://{host}/api/{path}', timeout=1) as response:
            payload = json.load(response)
        if not payload.get('success'):
            raise ValueError('Camera API rejected discovery')
        return payload['data']
    config = get('camera-config')
    ips = get('ip-config')
    if ips['masterIp'] != host or ips['slaveIp'] != link.address:
        raise ValueError('Camera IP setting does not match the current USB lease')
    namespace = config['cameraNamespace'].strip('/')
    if not namespace:
        raise ValueError('Camera namespace is empty')
    return {**vars(link), 'host': host, 'namespace': namespace}


def discover_cameras():
    def probe(link):
        try:
            return discover_camera(link), None
        except Exception as exc:
            return None, {'interface': link.name, 'error': str(exc)}
    links = camera_links()
    with ThreadPoolExecutor(max_workers=max(1, len(links))) as pool:
        results = list(pool.map(probe, links))
    return [camera for camera, _ in results if camera], [error for _, error in results if error]


def resolved_cameras(path=SNAPSHOT):
    """Reject stale discovery or leases replaced since the host's last scan."""
    payload = json.loads(path.read_text())
    age = time.monotonic() - payload['monotonic_s']
    if not 0 <= age <= 15:
        raise ValueError('USB camera discovery is stale')
    links = camera_links()
    return [camera for camera in payload['cameras']
            if CameraLink(camera['name'], camera['index'], camera['address']) in links]


def serve_discovery():
    """Publish verified USB peers and reconcile chrony's default-deny ACL."""
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['chronyc', 'deny', 'all', '0.0.0.0/0'], check=True, capture_output=True)
    allowed = set()
    while True:
        cameras, errors = discover_cameras()
        current = {camera['host'] for camera in cameras}
        for host in sorted(allowed - current):
            subprocess.run(['chronyc', 'deny', host], check=True, capture_output=True)
        # Reapply current peers so a chronyd restart needs no daemon restart.
        for host in sorted(current):
            subprocess.run(['chronyc', 'allow', host], check=True, capture_output=True)
        allowed = current
        payload = {'monotonic_s': time.monotonic(), 'cameras': cameras, 'errors': errors}
        temporary = SNAPSHOT.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload))
        temporary.chmod(0o644)
        os.replace(temporary, SNAPSHOT)
        time.sleep(2)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--urls', action='store_true')
    args = parser.parse_args()
    if args.serve:
        serve_discovery()
    else:
        cameras, errors = discover_cameras()
        if args.urls:
            print('\n'.join(sorted({f"http://{camera['host']}" for camera in cameras})))
        else:
            print(json.dumps({'cameras': cameras, 'errors': errors}, indent=2))
