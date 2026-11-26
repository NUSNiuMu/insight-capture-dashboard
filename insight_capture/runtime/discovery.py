"""Identify camera network generations independently of device IP assignments."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import ipaddress
import json
import math
import os
from pathlib import Path
import socket
import struct
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from urllib.request import ProxyHandler, build_opener, Request
import threading

SNAPSHOT = Path('/run/insight-capture-network/cameras.json')
_acquisition_locks = {}

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


def camera_link_generations(links):
    """Include carrier recoveries that retain the interface index and address."""
    generations = set()
    for link in links:
        try:
            count = int((Path("/sys/class/net") / link.name / "carrier_up_count").read_text())
        except (OSError, ValueError):
            count = None
        generations.add((link, count))
    return frozenset(generations)


def dhcp_peer(link):
    """Resolve the actual peer early, before the camera HTTP service starts."""
    result = subprocess.run(
        ['nmcli', '-t', '-f', 'DHCP4.OPTION', 'device', 'show', link.name],
        capture_output=True, text=True, check=True, timeout=3)
    options = dict(line.split(':', 1)[-1].split(' = ', 1)
                   for line in result.stdout.splitlines() if ' = ' in line)
    host = str(ipaddress.IPv4Address(options['dhcp_server_identifier']))
    network = ipaddress.IPv4Network(f"{link.address}/{options['subnet_mask']}", strict=False)
    if ipaddress.IPv4Address(host) not in network or host == link.address:
        raise ValueError('Camera DHCP server is not on its USB link')
    return host


def discover_camera(link, *, host=None):
    """Use the USB DHCP server and camera API, never a guessed .1 endpoint."""
    host = host or dhcp_peer(link)
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


STARTUP = SNAPSHOT.with_name('startup.json')
PHONE_CLOCK = SNAPSHOT.with_name('phone-clock.json')


def phone_clock_status():
    try:
        result = json.loads(PHONE_CLOCK.read_text())
        if result.get('state') == 'synced':
            return result
    except (OSError, ValueError, AttributeError):
        pass
    return {'state': 'idle', 'source': 'phone'}


def json_request(url, body=None, timeout=4):
    data = None if body is None else json.dumps(body).encode()
    request = Request(url, data=data, headers={'Content-Type': 'application/json'})
    with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
        return json.load(response)

def capture_busy(status):
    # operation_in_progress includes this request's own API transition lock.
    # The host separately holds the exclusive capture/upgrade lock.
    if type(status.get("recording")) is not bool:
        raise ValueError("Invalid recording status")
    busy_states = {"starting", "preparing", "recording", "saving", "finalizing", "merging", "stopping", "playback"}
    return (status["recording"] or status.get("state") in busy_states
            or status.get("merge_state") in busy_states
            or (status.get("current_take") or {}).get("state") in busy_states
            or status.get("playback_state") in {"playing", "playback"})


def recording_status():
    return json_request('http://127.0.0.1:8765/api/recording/status')


def startup_ready():
    try:
        return json.loads(STARTUP.read_text())
    except (OSError, ValueError):
        return {}

def recover_camera_startup():
    """Once per host boot, repair only acquisition with stale/missing input.

    ClockMonitor gates recording until this checkpoint exists. A service restart
    preserves it in /run; it never restarts a camera during an active recording.
    """
    completed = startup_ready()
    while True:
        try:
            recording = recording_status()
            if recording.get('managed_camera_startup') is False:
                time.sleep(5)
                continue
            if capture_busy(recording):
                time.sleep(2)
                continue
            status = json_request('http://127.0.0.1:8765/api/mobile/status')
            if not status.get('system', {}).get('managed_camera_startup'):
                time.sleep(5)
                continue
            if status['state'] in {'starting', 'recording', 'saving', 'playback'}:
                time.sleep(2)
                continue
            expected = {d['name'] for d in status['devices']}
            if expected and expected.issubset(completed):
                return
            peers = resolved_cameras()
            def recover(camera):
                name = camera['namespace']
                if name not in expected or name in completed or sum(p['namespace'] == name for p in peers) != 1:
                    return None
                url = f"http://{camera['host']}/api/"
                sync = json_request(url + 'time-sync-setting')['data']
                health = status['preflight'].get('camera_health', {}).get(name, {})
                epoch = status['preflight'].get('image_clocks', {}).get('devices', {}).get(name, {})
                action = 'already_ready'
                if not sync.get('synced') or not health.get('ready') or not epoch.get('ok'):
                    restart_acquisition(camera)
                    action = 'acquisition_restarted'
                return name, {'action': action, 'host': camera['host'], 'monotonic_s': time.monotonic()}
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [pool.submit(recover, camera) for camera in peers]
                for future in futures:
                    try:
                        result = future.result()
                        if result:
                            completed[result[0]] = result[1]
                            temp = STARTUP.with_suffix('.tmp')
                            temp.write_text(json.dumps(completed))
                            os.replace(temp, STARTUP)
                            print('Camera startup:', result, flush=True)
                    except Exception as exc:
                        print('Camera startup waiting:', str(exc), flush=True)
        except Exception as exc:
            print('Camera startup waiting:', str(exc), flush=True)
        time.sleep(2)


@contextmanager
def acquisition_lock(camera, timeout=-1):
    lock = _acquisition_locks.setdefault(camera["namespace"], threading.Lock())
    if not lock.acquire(timeout=timeout):
        raise RuntimeError("设备正在执行另一项采集恢复")
    try:
        yield
    finally:
        lock.release()


def timed_camera_request(timings, phase, url, body=None, *, timeout=4):
    started = time.monotonic()
    try:
        return json_request(url, body, timeout=timeout)
    except (OSError, ValueError) as exc:
        elapsed = round((time.monotonic() - started) * 1000)
        raise RuntimeError(f"{phase} ({elapsed} ms): {exc}") from exc
    finally:
        timings[phase + "_ms"] = round((time.monotonic() - started) * 1000)


def restart_acquisition(camera, *, lock_timeout=-1):
    """Start uses the camera's existing stopped-acquisition clock step gate."""
    timings = {}
    with acquisition_lock(camera, lock_timeout):
        for operation in ("insight-stop", "insight-start"):
            result = timed_camera_request(timings, operation, f"http://{camera['host']}/api/{operation}", {}, timeout=165)
            if not result.get("success"):
                raise RuntimeError(f"{operation}: {result}")
    return timings


def recover_camera_clock(camera):
    if camera["namespace"] != "egohalo":
        return restart_acquisition(camera, lock_timeout=5)

    # Refresh capture timestamps, then use the device's USB NTP for precision.
    # The native /api/time also holds the device recorder lease.
    host = camera["host"]
    setting_url = f"http://{host}/api/time-sync-setting"
    timings = {}
    with acquisition_lock(camera, 5):
        setting = timed_camera_request(timings, "ntp_setting_check", setting_url)
        if not setting["data"]["enabled"]:
            raise RuntimeError("EgoHalo 的 USB NTP 校时未开启")
        result = timed_camera_request(timings, "image_clock_request", f"http://{host}:8765/api/time",
                                     {"unix_ms": time.time_ns() // 1_000_000}, timeout=8)
        if not result.get("time", {}).get("valid"):
            raise RuntimeError("EgoHalo 图像时钟校时失败")
        if not timed_camera_request(timings, "ntp_request", setting_url, {"enabled": True}).get("success"):
            raise RuntimeError("EgoHalo USB NTP 校时请求失败")
    return timings


def synchronize_phone_clock(body):
    """A fixed idle-only operation; never accept commands or paths from a client."""
    epoch, received = body.get('epoch_s'), body.get('received_monotonic_s')
    if (type(epoch) not in (int, float) or not math.isfinite(epoch)
            or not 946684800 <= epoch < 4102444800
            or type(received) not in (int, float) or not math.isfinite(received)
            or not 0 <= time.monotonic() - received <= 5):
        raise ValueError('Phone clock sample is invalid or expired')
    resync = body.get('resync_cameras', False)
    if type(resync) is not bool:
        raise ValueError('resync_cameras must be a boolean')
    targets = body.get('camera_hosts')
    if targets is not None and (
        not resync or not isinstance(targets, list) or not targets
        or not all(isinstance(host, str) for host in targets)
    ):
        raise ValueError('Invalid camera recovery targets')
    timings = {}
    started = time.monotonic()
    mounts = json.loads(subprocess.run(
        ['docker', 'inspect', '--format', '{{json .Mounts}}', 'insight-dashboard'],
        capture_output=True, text=True, check=True, timeout=5).stdout)
    config = None
    for mount in mounts:
        if mount['Type'] != 'bind':
            continue
        if mount['Destination'] == '/workspaces/insight_capture/config':
            config = Path(mount['Source'])
            break
        if mount['Destination'] == '/workspaces/insight_capture':
            config = Path(mount['Source']) / 'config'
    if config is None:
        raise RuntimeError('Capture configuration is not mounted')
    descriptor = os.open(config / '.capture-upgrade.lock', os.O_RDONLY | os.O_CREAT | os.O_CLOEXEC, 0o644)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not resync:
            previous = phone_clock_status()
            if previous['state'] == 'synced':
                return previous
        timings["lock_and_mount_ms"] = round((time.monotonic() - started) * 1000)
        started = time.monotonic()
        busy = capture_busy(recording_status())
        timings["recording_status_ms"] = round((time.monotonic() - started) * 1000)
        if busy:
            return {'state': 'deferred', 'timings_ms': timings}
        offset = epoch + time.monotonic() - received - time.time()
        adjusted = not resync and abs(offset) > 1
        if adjusted:
            time.clock_settime(time.CLOCK_REALTIME, time.time() + offset)
        result = {'state': 'synced', 'synced_at_epoch_s': time.time(), 'correction_s': offset}
        if adjusted or resync:
            started = time.monotonic()
            recovery_errors = {}
            try:
                cameras = resolved_cameras()
            except (OSError, ValueError, KeyError) as exc:
                cameras = []
                recovery_errors["discovery"] = str(exc)
            if targets is not None:
                cameras = [camera for camera in cameras if camera["host"] in targets]
                if {camera["host"] for camera in cameras} != set(targets) or len(cameras) != len(set(targets)):
                    raise RuntimeError("待校时相机已离线或 USB 连接已变化")
            camera_timings = {}
            if resync:
                result["camera_timings_ms"] = camera_timings
            operation = recover_camera_clock if resync else restart_acquisition
            workers = max(1, len(cameras)) if resync else 3
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [(camera["namespace"], pool.submit(operation, camera)) for camera in cameras]
                # Finish all camera work before releasing capture admission.
                for name, future in futures:
                    try:
                        camera_timings[name] = future.result()
                    except Exception as exc:
                        recovery_errors[name] = str(exc)
            timings["camera_requests_ms"] = round((time.monotonic() - started) * 1000)
            if resync:
                result["timings_ms"] = timings
                print("Camera time recovery:", json.dumps({"timings_ms": timings, "cameras": camera_timings,
                                                            "errors": recovery_errors}), flush=True)
            if recovery_errors:
                if resync:
                    raise RuntimeError("; ".join(f"{name}: {error}" for name, error in recovery_errors.items()))
                result["camera_recovery_errors"] = recovery_errors
        if not resync:
            # /run survives service restarts, and is recreated on the next host boot.
            result.update(source='phone', adjusted=adjusted)
            temporary = PHONE_CLOCK.with_suffix('.tmp')
            temporary.write_text(json.dumps(result))
            temporary.chmod(0o644)
            os.replace(temporary, PHONE_CLOCK)
        return result
    finally:
        os.close(descriptor)


def serve_phone_clock():
    """Root-only Unix socket, shared through the existing read-only runtime mount."""
    path = SNAPSHOT.with_name('phone-clock.sock')
    path.unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen(2)
        while True:
            connection, _ = listener.accept()
            with connection:
                try:
                    _, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid != 0:
                        raise PermissionError('Only the local capture service may set the clock')
                    connection.settimeout(3)
                    with connection.makefile('rb') as stream:
                        data = stream.readline(4096)
                    body = json.loads(data)
                    if not isinstance(body, dict):
                        raise ValueError('Expected a clock sample')
                    result = synchronize_phone_clock(body)
                except Exception as exc:
                    result = {'state': 'error', 'error': str(exc)}
                try:
                    connection.sendall(json.dumps(result).encode() + b'\n')
                except OSError:
                    pass  # Recovery is complete even when the caller disconnected.


def serve_discovery():
    """Publish verified USB peers and reconcile chrony's default-deny ACL."""
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['chronyc', 'deny', 'all', '0.0.0.0/0'], check=True, capture_output=True)
    allowed = set()
    threading.Thread(target=recover_camera_startup, daemon=True).start()
    threading.Thread(target=serve_phone_clock, daemon=True).start()
    while True:
        # NTP must be available before the camera API/acquisition can start.
        # Trust only current CDC-NCM DHCP leases, never STA/AP or guessed IPs.
        current = set()
        for link in camera_links():
            try:
                current.add(dhcp_peer(link))
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                pass
        for host in sorted(allowed - current):
            subprocess.run(['chronyc', 'deny', host], check=True, capture_output=True)
        # Reapply current peers so a chronyd restart needs no daemon restart.
        for host in sorted(current):
            subprocess.run(['chronyc', 'allow', host], check=True, capture_output=True)
        allowed = current
        cameras, errors = discover_cameras()
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
