"""Measure device clocks against the recorder without changing either clock."""

from __future__ import annotations

import concurrent.futures
import socket
import re
import ipaddress
import subprocess
import struct
import time
from insight_capture.runtime.discovery import resolved_cameras


NTP_EPOCH = 2_208_988_800


def ntp_sample(host: str, timeout: float = 0.5) -> dict:
    """Validate an NTP reply and retain the four timestamps used for its estimate."""
    packet = bytearray(48)
    packet[0] = 0x23
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.connect((host, 123))
        # Linux SO_TIMESTAMPNS removes Python scheduling delay after packet arrival.
        timestamp_option = getattr(socket, "SO_TIMESTAMPNS", 35)
        sock.setsockopt(socket.SOL_SOCKET, timestamp_option, 1)
        sent = time.time()
        mono = time.monotonic()
        ntp = sent + NTP_EPOCH
        struct.pack_into("!II", packet, 40, int(ntp), int(ntp % 1 * 2**32))
        sock.send(packet)
        reply, ancillary, _, _ = sock.recvmsg(512, 128)
        wall_after = time.time()
        received = wall_after
        for level, kind, value in ancillary:
            if level == socket.SOL_SOCKET and kind == timestamp_option:
                sec, ns = struct.unpack("@ll", value[:struct.calcsize("@ll")])
                received = sec + ns / 1e9
    elapsed = time.monotonic() - mono
    if len(reply) < 48 or reply[0] & 7 != 4 or reply[0] >> 6 == 3:
        raise ValueError("NTP server is unsynchronized or returned an invalid reply")
    if not 1 <= reply[1] <= 15 or reply[24:32] != packet[40:48]:
        raise ValueError("NTP stratum/origin validation failed")
    if abs(wall_after - sent - elapsed) > 0.005:
        raise ValueError("Jetson wall clock stepped during NTP measurement")
    values = struct.unpack("!12I", reply[:48])
    server_rx = values[8] - NTP_EPOCH + values[9] / 2**32
    server_tx = values[10] - NTP_EPOCH + values[11] / 2**32
    delay = received - sent - (server_tx - server_rx)
    if not values[10] or delay < -0.001 or server_tx < server_rx:
        raise ValueError("Invalid NTP server timestamps")
    offset = ((server_rx - sent) + (server_tx - received)) / 2
    return {
        "host": host, "checked_at_epoch_s": received, "stratum": reply[1],
        "offset_ms": round(offset * 1000, 4),
        "round_trip_ms": round(max(0, delay) * 1000, 4),
        "offset_bound_ms": round((abs(offset) + max(0, delay) / 2) * 1000, 4),
        "timestamps_epoch_s": [sent, server_rx, server_tx, received],
    }


def ssh_ntp_sample(device):
    """Query a client-only camera's ntpdate; no clock-setting options are used."""
    host = str(ipaddress.ip_address(device["host"]))
    server = str(ipaddress.ip_address(device["ntp_server"]))
    alias = device.get('ssh_host_key_alias')
    completed = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
         "-o", "ConnectTimeout=3", "-o", "ConnectionAttempts=1",
         *(['-o', f'HostKeyAlias={alias}'] if alias else []),
         "-i", device.get("ssh_identity", "/root/.ssh/insight_clock_ed25519"), f"root@{host}",
         f"ntpdate -q -p 3 -t 1 {server}"],
        capture_output=True, text=True, timeout=12, check=False)
    output = completed.stdout + completed.stderr
    match = re.search(r"server [^,]+, stratum (\d+), offset ([+-]?[\d.]+), delay ([\d.]+)", output)
    if completed.returncode or not match or not 1 <= int(match[1]) <= 15:
        raise ValueError("Camera NTP query failed: " + output.strip()[-400:])
    # ntpdate reports server-minus-camera; normalize to camera-minus-Jetson.
    offset, delay = -float(match[2]) * 1000, float(match[3]) * 1000
    return {"host": host, "checked_at_epoch_s": time.time(), "stratum": int(match[1]),
            "method": "camera ntpdate -q, three samples; camera minus Jetson",
            "offset_ms": round(offset, 4), "round_trip_ms": round(delay, 4),
            "offset_bound_ms": round(abs(offset) + max(0, delay) / 2, 4)}


def measure_clocks(config: dict) -> dict:
    """Use each device's least delayed sample; fail closed on missing clocks."""
    devices = config["clock_devices"]
    peers = resolved_cameras() if config.get('discover_usb') else None
    limit = float(config.get("max_clock_bound_ms", 10))
    rtt_limit = float(config.get("max_ntp_rtt_ms", 10))

    def measure(device):
        device = dict(device)
        if peers is not None:
            matches = [peer for peer in peers if peer['namespace'] == device['name']]
            if len(matches) != 1:
                return device['name'], {'ok': False, 'error': f'Expected one USB camera named {device["name"]}, found {len(matches)}'}
            device.update(host=matches[0]['host'], ntp_server=matches[0]['address'])
        samples, errors = [], []
        for _ in range(1 if device.get("method") == "ssh_ntpdate" else int(config.get("ntp_samples", 3))):
            try:
                samples.append(ssh_ntp_sample(device) if device.get("method") == "ssh_ntpdate" else ntp_sample(device["host"]))
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                errors.append(str(exc))
        if not samples:
            return device["name"], {"ok": False, "host": device["host"], "error": "; ".join(errors)}
        best = min(samples, key=lambda sample: sample["round_trip_ms"])
        best.update(ok=best["offset_bound_ms"] <= float(device.get("max_clock_bound_ms", limit))
                    and best["round_trip_ms"] <= float(device.get("max_ntp_rtt_ms", rtt_limit)))
        return device["name"], best

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(devices))) as executor:
        results = dict(executor.map(measure, devices))
    bounds = sorted((item.get("offset_bound_ms", float("inf")) for item in results.values()), reverse=True)
    pairwise_bound = sum(bounds[:2]) if bounds else None
    pairwise_limit = float(config.get("max_pairwise_clock_bound_ms", 20))
    return {
        "method": "NTP measurement; device minus Jetson; method recorded per device",
        "checked_at_epoch_s": time.time(), "max_clock_bound_ms": limit,
        "max_ntp_rtt_ms": rtt_limit, "devices": results,
        "pairwise_clock_bound_ms": round(pairwise_bound, 4) if pairwise_bound is not None and pairwise_bound != float("inf") else None,
        "max_pairwise_clock_bound_ms": pairwise_limit,
        "ok": bool(results) and all(item["ok"] for item in results.values()) and pairwise_bound <= pairwise_limit,
    }


class ClockMonitor:
    """Read-only camera clock measurements; never changes a recording clock."""
    def __init__(self, config):
        import threading
        self.config = config
        self._lock = threading.Lock()
        self._report = {"ok": False, "devices": {}, "error": "Waiting for NTP measurement"}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="camera_ntp_monitor")
        self._thread.start()

    def measure(self):
        report = measure_clocks(self.config)
        with self._lock:
            self._report = report
        return report

    def status(self):
        with self._lock:
            report = dict(self._report)
        if self.config.get('discover_usb'):
            try:
                peers = resolved_cameras()
                for name, result in report.get('devices', {}).items():
                    hosts = [peer['host'] for peer in peers if peer['namespace'] == name]
                    if hosts != [result.get('host')]:
                        report.update(ok=False, error='USB camera changed; waiting for a fresh NTP measurement')
            except (OSError, ValueError, KeyError) as exc:
                report.update(ok=False, error=str(exc))
        age = max(0, time.time() - report.get("checked_at_epoch_s", 0))
        return {**report, "age_sec": age, "ok": bool(report.get("ok")) and age <= 30}

    def _run(self):
        while not self._stop.is_set():
            try:
                self.measure()
            except Exception as exc:
                with self._lock:
                    self._report = {"ok": False, "error": str(exc), "devices": {}}
            self._stop.wait(10)

    def close(self):
        self._stop.set()
