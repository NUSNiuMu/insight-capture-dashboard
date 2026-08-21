#!/usr/bin/env python3
"""Continuously record evidence needed to explain future camera outages.

The monitor is intentionally read-only.  It correlates the Dashboard camera
API, camera HTTP endpoints, USB-network interfaces, process changes, and the
kernel journal into one JSONL timeline under outputs/.
"""

from __future__ import annotations

import argparse
from collections import deque
import dataclasses
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import socket
import struct
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config" / "cameras.json"
DEFAULT_OUTPUT_DIR = ROOT / "outputs" / "camera_diagnostics"
DEFAULT_CAMERA_IPS = {
    "insight3_a": "169.254.10.1",
    "insight3_b": "169.254.20.1",
    "insight9_a": "169.254.30.1",
}
KERNEL_PATTERN = re.compile(
    r"usb|cdc_ncm|tegra-xusb|enx02000000(?:0a|14|1e)01|carrier|link becomes",
    re.IGNORECASE,
)
IDENTITY_PATTERN = re.compile(r"(enx[0-9a-f]+|usb\s+\d+-\d+(?:\.\d+)*)", re.IGNORECASE)


@dataclasses.dataclass(frozen=True)
class Condition:
    severity: str
    cause: str
    summary: str
    evidence: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    sustain_sec: float | None = None


class JsonlWriter:
    """Thread-safe, line-buffered event writer."""

    def __init__(self, path: Path, *, max_bytes: int = 100 * 1024 * 1024) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.base_path = path
        self.path = path
        self.max_bytes = max_bytes
        self.part = 1
        self._stream = path.open("a", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()

    def _rotate_if_needed(self) -> None:
        if self._stream.tell() < self.max_bytes:
            return
        self._stream.close()
        self.part += 1
        self.path = self.base_path.with_name(
            f"{self.base_path.stem}-part{self.part}{self.base_path.suffix}"
        )
        self._stream = self.path.open("a", encoding="utf-8", buffering=1)

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        now = dt.datetime.now().astimezone()
        record = {
            "ts": now.isoformat(timespec="milliseconds"),
            "epoch_s": round(now.timestamp(), 3),
            "event": event,
            **fields,
        }
        encoded = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self._rotate_if_needed()
            self._stream.write(encoded + "\n")
        if fields.get("severity") in {"warning", "critical"}:
            print(encoded, flush=True)
        return record

    def close(self) -> None:
        with self._lock:
            self._stream.close()


class InstanceLock:
    """Prevent duplicate monitors from distorting counters and logs."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._stream = path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._stream.seek(0)
            owner = self._stream.read().strip() or "unknown"
            self._stream.close()
            raise RuntimeError(f"camera monitor already running with pid {owner}") from exc
        self._stream.seek(0)
        self._stream.truncate()
        self._stream.write(f"{os.getpid()}\n")
        self._stream.flush()

    def close(self) -> None:
        try:
            self._stream.seek(0)
            owner = self._stream.read().strip()
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            self._stream.close()
            if owner == str(os.getpid()):
                self.path.unlink(missing_ok=True)
        except OSError:
            pass


class SustainedEventTracker:
    """Emit one start and one resolution for sustained conditions."""

    def __init__(self, default_sustain_sec: float = 3.0) -> None:
        self.default_sustain_sec = default_sustain_sec
        self.pending_since: dict[str, float] = {}
        self.active: dict[str, Condition] = {}

    def update(
        self,
        conditions: Mapping[str, Condition],
        now: float,
    ) -> list[tuple[str, str, Condition, float]]:
        events: list[tuple[str, str, Condition, float]] = []
        for code, previous in list(self.active.items()):
            if code in conditions:
                continue
            events.append(("resolved", code, previous, 0.0))
            del self.active[code]
            self.pending_since.pop(code, None)

        for code in list(self.pending_since):
            if code not in conditions:
                del self.pending_since[code]

        for code, condition in conditions.items():
            if code in self.active:
                self.active[code] = condition
                continue
            started = self.pending_since.setdefault(code, now)
            sustain = (
                self.default_sustain_sec
                if condition.sustain_sec is None
                else condition.sustain_sec
            )
            age = now - started
            if age < sustain:
                continue
            self.active[code] = condition
            events.append(("started", code, condition, age))
        return events


class KernelEventCorrelator:
    """Recognize camera-link kernel events and multi-device USB resets."""

    def __init__(self, cluster_window_sec: float = 5.0) -> None:
        self.cluster_window_sec = cluster_window_sec
        self.recent_disconnects: deque[tuple[float, str]] = deque()
        self.last_cluster: frozenset[str] = frozenset()

    def observe(self, line: str, now: float) -> list[dict[str, Any]]:
        lower = line.lower()
        result: list[dict[str, Any]] = []
        if "usb disconnect" in lower or ("cdc_ncm" in lower and "unregister" in lower):
            event = "usb_camera_disconnect"
            cause = "usb_link_or_power_removed"
            severity = "critical"
        elif "new superspeed usb device" in lower or (
            "cdc_ncm" in lower and "register 'cdc_ncm'" in lower
        ):
            event = "usb_camera_reenumerated"
            cause = "usb_device_returned"
            severity = "info"
        elif "trb" in lower or "tegra-xusb" in lower and "warn" in lower:
            event = "usb_controller_warning"
            cause = "usb_controller_transport_error"
            severity = "warning"
        elif "link becomes ready" in lower:
            event = "camera_network_link_ready"
            cause = "usb_network_recovered"
            severity = "info"
        else:
            event = "kernel_camera_event"
            cause = "kernel_observation"
            severity = "info"

        match = IDENTITY_PATTERN.search(line)
        identity = match.group(1).replace(" ", "_") if match else "unknown"
        result.append(
            {
                "event": event,
                "severity": severity,
                "cause": cause,
                "identity": identity,
                "kernel_line": line.rstrip(),
            }
        )

        if event != "usb_camera_disconnect":
            return result
        self.recent_disconnects.append((now, identity))
        while self.recent_disconnects and now - self.recent_disconnects[0][0] > self.cluster_window_sec:
            self.recent_disconnects.popleft()
        identities = frozenset(item[1] for item in self.recent_disconnects)
        if len(identities) >= 2 and identities != self.last_cluster:
            self.last_cluster = identities
            result.append(
                {
                    "event": "usb_disconnect_cluster",
                    "severity": "critical",
                    "cause": "shared_usb_hub_power_or_controller_reset",
                    "identities": sorted(identities),
                    "summary": "Multiple camera USB links disappeared within 5 seconds.",
                }
            )
        return result


def _get_json(url: str, timeout: float) -> tuple[dict[str, Any] | None, str | None]:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "insight-camera-monitor/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            return None, f"unexpected payload type {type(payload).__name__}"
        return payload, None
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _link_local_interfaces() -> dict[str, str]:
    """Return peer IP -> interface without requiring the `ip` command."""

    peers: dict[str, str] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _index, name in socket.if_nameindex():
            try:
                packed = fcntl.ioctl(
                    sock.fileno(),
                    0x8915,  # SIOCGIFADDR
                    struct.pack("256s", name.encode()[:15]),
                )
            except OSError:
                continue
            address = socket.inet_ntoa(packed[20:24])
            octets = address.split(".")
            if len(octets) != 4 or octets[:2] != ["169", "254"]:
                continue
            peer_last = "2" if octets[3] == "1" else "1"
            peers[".".join((*octets[:3], peer_last))] = name
    finally:
        sock.close()
    return peers


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _interface_snapshot(name: str | None) -> dict[str, Any]:
    if not name:
        return {"name": None, "present": False}
    root = Path("/sys/class/net") / name
    if not root.exists():
        return {"name": name, "present": False}
    stats = root / "statistics"
    return {
        "name": name,
        "present": True,
        "operstate": _read_text(root / "operstate"),
        "carrier": _read_int(root / "carrier"),
        "rx_bytes": _read_int(stats / "rx_bytes"),
        "tx_bytes": _read_int(stats / "tx_bytes"),
        "rx_errors": _read_int(stats / "rx_errors"),
        "rx_dropped": _read_int(stats / "rx_dropped"),
        "tx_errors": _read_int(stats / "tx_errors"),
        "tx_dropped": _read_int(stats / "tx_dropped"),
    }


def _with_counter_deltas(current: dict[str, Any], previous: Mapping[str, Any] | None) -> dict[str, Any]:
    result = dict(current)
    previous = previous or {}
    for key in ("rx_bytes", "tx_bytes", "rx_errors", "rx_dropped", "tx_errors", "tx_dropped"):
        value = current.get(key)
        old = previous.get(key)
        result[f"{key}_delta"] = max(0, value - old) if isinstance(value, int) and isinstance(old, int) else None
    return result


def _unwrap_data(payload: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not payload:
        return {}
    data = payload.get("data")
    return data if isinstance(data, Mapping) else payload


def _parse_expected_fps(payload: Mapping[str, Any] | None) -> float | None:
    data = _unwrap_data(payload)
    value = data.get("fps")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*fps", str(value), re.IGNORECASE)
    return float(match.group(1)) if match else None


def _reported_camera_name(probe: Mapping[str, Any]) -> str | None:
    config = probe.get("camera_config")
    if not isinstance(config, Mapping):
        return None
    value = config.get("cameraNamespace")
    return str(value) if value else None


def map_probes_by_camera_identity(
    camera_names: Iterable[str],
    probes_by_ip: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Mapping[str, Any]], dict[str, list[str]]]:
    """Map changing peer IPs to configured cameras using camera-reported identity."""

    allowed = set(camera_names)
    claims: dict[str, list[Mapping[str, Any]]] = {name: [] for name in allowed}
    for probe in probes_by_ip.values():
        reported = _reported_camera_name(probe)
        if reported in allowed:
            claims[reported].append(probe)

    assignments: dict[str, Mapping[str, Any]] = {}
    conflicts: dict[str, list[str]] = {}
    for name, matches in claims.items():
        if not matches:
            continue
        ranked = sorted(
            matches,
            key=lambda item: (not bool(item.get("http_ok")), str(item.get("ip"))),
        )
        assignments[name] = ranked[0]
        live_ips = sorted(
            str(item.get("ip")) for item in matches if item.get("http_ok")
        )
        if len(live_ips) >= 2:
            conflicts[name] = live_ips
    return assignments, conflicts


def _camera_input_fps(row: Mapping[str, Any]) -> float:
    stats = row.get("webrtc_stats")
    if isinstance(stats, Mapping):
        value = stats.get("input_fps")
        if isinstance(value, (int, float)):
            return float(value)
    value = row.get("fps")
    return float(value) if isinstance(value, (int, float)) else 0.0


def _runtime_pids() -> list[int]:
    result: list[int] = []
    proc = Path("/proc")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if _is_dashboard_runtime_command(command):
            result.append(int(entry.name))
    return sorted(result)


def _is_dashboard_runtime_command(command: bytes) -> bool:
    parts = [part.decode(errors="replace") for part in command.split(b"\0") if part]
    if not parts or not Path(parts[0]).name.startswith("python"):
        return False
    return any(
        parts[index : index + 2] == ["-m", "insight_capture.runtime.app"]
        for index in range(len(parts) - 1)
    )


def derive_conditions(snapshot: Mapping[str, Any], *, low_fps_ratio: float = 0.8) -> dict[str, Condition]:
    """Turn a cross-layer snapshot into actionable fault hypotheses."""

    conditions: dict[str, Condition] = {}
    cameras = snapshot.get("cameras")
    camera_rows = cameras if isinstance(cameras, Mapping) else {}
    dashboard = snapshot.get("dashboard")
    dashboard = dashboard if isinstance(dashboard, Mapping) else {}

    down: list[str] = []
    for name, camera in camera_rows.items():
        if not isinstance(camera, Mapping):
            continue
        if camera.get("discovered") is False:
            continue
        interface = camera.get("interface")
        interface = interface if isinstance(interface, Mapping) else {}
        if not interface.get("present") or interface.get("carrier") == 0:
            down.append(str(name))
    shared_link_failure = len(down) >= 2
    if shared_link_failure:
        conditions["usb.shared_link_failure"] = Condition(
            severity="critical",
            cause="shared_usb_hub_power_or_controller_reset",
            summary="Multiple camera USB-network links are down together.",
            evidence={"cameras": sorted(down)},
            sustain_sec=0.0,
        )

    identity_conflicts = snapshot.get("camera_identity_conflicts")
    if isinstance(identity_conflicts, Mapping):
        for name, peers in identity_conflicts.items():
            conditions[f"camera.{name}.duplicate_identity"] = Condition(
                severity="critical",
                cause="duplicate_camera_namespace_on_multiple_live_peers",
                summary=f"Multiple live camera peers report identity {name}.",
                evidence={"camera": name, "peers": peers},
                sustain_sec=0.0,
            )

    reachable = [
        str(name)
        for name, camera in camera_rows.items()
        if isinstance(camera, Mapping) and camera.get("http_ok")
    ]
    if not dashboard.get("ok"):
        conditions["dashboard.api_unreachable"] = Condition(
            severity="critical",
            cause="dashboard_backend_stopped_or_unreachable",
            summary="Dashboard API is unavailable while camera peers are still probed.",
            evidence={"error": dashboard.get("error"), "reachable_cameras": reachable},
        )
        return conditions

    runtime_pids = snapshot.get("dashboard_runtime_pids")
    if isinstance(runtime_pids, list) and len(runtime_pids) != 1:
        conditions["dashboard.runtime_process_count"] = Condition(
            severity="critical" if not runtime_pids else "warning",
            cause=(
                "dashboard_backend_process_missing"
                if not runtime_pids
                else "overlapping_or_duplicate_dashboard_backends"
            ),
            summary=(
                "No Dashboard runtime process is visible."
                if not runtime_pids
                else "More than one Dashboard runtime process is visible."
            ),
            evidence={"pids": runtime_pids},
            sustain_sec=2.0,
        )

    api_cameras = dashboard.get("cameras")
    api_cameras = api_cameras if isinstance(api_cameras, Mapping) else {}
    expected_domain = snapshot.get("expected_ros_domain_id")
    for name, camera in camera_rows.items():
        if not isinstance(camera, Mapping):
            continue
        if camera.get("discovered") is False:
            conditions[f"camera.{name}.not_discovered"] = Condition(
                severity="critical",
                cause="camera_updating_powered_off_or_not_on_any_discovered_peer",
                summary=f"No discovered peer currently reports camera identity {name}.",
                evidence={
                    "camera": name,
                    "candidate_peers": camera.get("candidate_peers"),
                },
            )
            continue
        interface = camera.get("interface")
        interface = interface if isinstance(interface, Mapping) else {}
        if not shared_link_failure and (
            not interface.get("present") or interface.get("carrier") == 0
        ):
            conditions[f"camera.{name}.usb_link_down"] = Condition(
                severity="critical",
                cause="camera_usb_link_or_power_lost",
                summary=f"{name} USB-network link is down.",
                evidence={"interface": dict(interface)},
                sustain_sec=0.0,
            )
            continue

        if interface.get("present"):
            error_deltas = {
                key: interface.get(key)
                for key in ("rx_errors_delta", "rx_dropped_delta", "tx_errors_delta", "tx_dropped_delta")
                if isinstance(interface.get(key), int) and interface.get(key) > 0
            }
            if error_deltas:
                conditions[f"camera.{name}.network_errors"] = Condition(
                    severity="warning",
                    cause="usb_network_packet_errors_or_congestion",
                    summary=f"{name} USB-network error counters are increasing.",
                    evidence=error_deltas,
                    sustain_sec=0.0,
                )

        if not camera.get("http_ok"):
            conditions[f"camera.{name}.http_unreachable"] = Condition(
                severity="critical",
                cause="camera_boot_service_or_ip_path_failure",
                summary=f"{name} camera HTTP service is unreachable while its host link exists.",
                evidence={"ip": camera.get("ip"), "error": camera.get("http_error")},
            )
            continue

        metadata_errors = camera.get("metadata_errors")
        if isinstance(metadata_errors, Mapping) and metadata_errors:
            conditions[f"camera.{name}.control_api_degraded"] = Condition(
                severity="warning",
                cause="camera_control_service_degraded_or_restarting",
                summary=f"{name} responds to health probes but some control APIs fail.",
                evidence={"errors": dict(metadata_errors)},
                sustain_sec=10.0,
            )

        domain = camera.get("ros_domain_id")
        if isinstance(expected_domain, int) and isinstance(domain, int) and domain != expected_domain:
            conditions[f"camera.{name}.ros_domain_mismatch"] = Condition(
                severity="critical",
                cause="ros_domain_configuration_mismatch",
                summary=f"{name} ROS Domain does not match the Dashboard configuration.",
                evidence={"camera_domain": domain, "expected_domain": expected_domain},
                sustain_sec=0.0,
            )

        row = api_cameras.get(name)
        if not isinstance(row, Mapping):
            conditions[f"camera.{name}.missing_from_dashboard"] = Condition(
                severity="critical",
                cause="dashboard_camera_configuration_or_subscription_missing",
                summary=f"{name} is absent from the Dashboard camera API.",
                evidence={"api_cameras": sorted(str(item) for item in api_cameras)},
            )
            continue

        age = row.get("input_age_sec")
        stale = bool(row.get("stale")) or age is None
        if stale:
            rx_delta = interface.get("rx_bytes_delta")
            transport_alive = isinstance(rx_delta, int) and rx_delta > 0
            cause = (
                "image_publisher_or_dds_large_payload_stalled"
                if transport_alive
                else "camera_ros_data_path_stalled"
            )
            summary = (
                f"{name} has no images although the camera and USB traffic remain alive."
                if transport_alive
                else f"{name} has no images and no USB receive traffic was observed."
            )
            conditions[f"camera.{name}.image_stale"] = Condition(
                severity="critical",
                cause=cause,
                summary=summary,
                evidence={
                    "input_age_sec": age,
                    "stale": row.get("stale"),
                    "version": row.get("version"),
                    "rx_bytes_delta": rx_delta,
                    "camera_http_ok": True,
                },
            )
            continue

        source_fps = _camera_input_fps(row)
        expected_fps = camera.get("expected_fps")
        if (
            isinstance(expected_fps, (int, float))
            and expected_fps > 0
            and source_fps > 0
            and source_fps < float(expected_fps) * low_fps_ratio
        ):
            conditions[f"camera.{name}.fps_degraded"] = Condition(
                severity="warning",
                cause="usb_bandwidth_cpu_or_image_pipeline_degradation",
                summary=f"{name} image rate is below {low_fps_ratio:.0%} of its configured rate.",
                evidence={"source_fps": source_fps, "expected_fps": expected_fps},
                sustain_sec=10.0,
            )
    return conditions


class CameraFailureMonitor:
    def __init__(self, args: argparse.Namespace, writer: JsonlWriter) -> None:
        self.args = args
        self.writer = writer
        self.stop_event = threading.Event()
        self.tracker = SustainedEventTracker(args.sustain_sec)
        self.kernel_correlator = KernelEventCorrelator()
        self.previous_interfaces: dict[str, dict[str, Any]] = {}
        self.previous_runtime_pids: list[int] | None = None
        self.last_snapshot_log = 0.0
        self.last_metadata_poll = 0.0
        self.peer_metadata: dict[str, dict[str, Any]] = {}
        self.previous_camera_ips: dict[str, str] = {}
        self.kernel_process: subprocess.Popen[str] | None = None
        self.kernel_thread: threading.Thread | None = None
        self.config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        self.camera_names = [
            str(camera["name"])
            for camera in self.config.get("cameras", [])
            if isinstance(camera, dict) and camera.get("enabled", True)
        ]
        self.expected_domain = int(self.config.get("ros_domain_id", 0))

    def _start_kernel_journal(self) -> None:
        if self.args.no_kernel_journal:
            return
        try:
            self.kernel_process = subprocess.Popen(
                ["journalctl", "-k", "-f", "-n", "0", "-o", "short-iso"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            self.writer.emit(
                "collector_unavailable",
                severity="warning",
                collector="kernel_journal",
                error=str(exc),
            )
            return
        self.kernel_thread = threading.Thread(
            target=self._kernel_loop,
            daemon=True,
            name="camera_kernel_journal",
        )
        self.kernel_thread.start()

    def _kernel_loop(self) -> None:
        process = self.kernel_process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            if self.stop_event.is_set():
                return
            if not KERNEL_PATTERN.search(line):
                continue
            now = time.monotonic()
            for event in self.kernel_correlator.observe(line, now):
                name = str(event.pop("event"))
                self.writer.emit(name, **event)

    def _camera_probe(self, ip: str, include_metadata: bool) -> dict[str, Any]:
        version, version_error = _get_json(f"http://{ip}/api/version", self.args.http_timeout)
        result: dict[str, Any] = {
            "ip": ip,
            "http_ok": version is not None,
            "http_error": version_error,
            "version": version.get("version") if version else None,
        }
        if not include_metadata or version is None:
            return result
        domain, domain_error = _get_json(
            f"http://{ip}/api/ros-domain-id", self.args.http_timeout
        )
        fps, fps_error = _get_json(
            f"http://{ip}/api/camera-fps", self.args.http_timeout
        )
        config, config_error = _get_json(
            f"http://{ip}/api/camera-config", self.args.http_timeout
        )
        domain_data = _unwrap_data(domain)
        try:
            result["ros_domain_id"] = int(domain_data.get("rosDomainId"))
        except (TypeError, ValueError):
            result["ros_domain_id"] = None
        result["expected_fps"] = _parse_expected_fps(fps)
        result["camera_config"] = dict(_unwrap_data(config))
        result["metadata_errors"] = {
            key: value
            for key, value in {
                "domain": domain_error,
                "fps": fps_error,
                "config": config_error,
            }.items()
            if value
        }
        return result

    def _merge_peer_metadata(
        self,
        ip: str,
        probe: Mapping[str, Any],
        include_metadata: bool,
    ) -> dict[str, Any]:
        if include_metadata and probe.get("http_ok"):
            cached = dict(self.peer_metadata.get(ip, {}))
            camera_config = probe.get("camera_config")
            if isinstance(camera_config, Mapping) and camera_config:
                cached["camera_config"] = dict(camera_config)
            for key in ("ros_domain_id", "expected_fps"):
                if probe.get(key) is not None:
                    cached[key] = probe.get(key)
            self.peer_metadata[ip] = cached
        merged = dict(self.peer_metadata.get(ip, {}))
        for key, value in probe.items():
            if key == "camera_config" and not value:
                continue
            if key in {"ros_domain_id", "expected_fps"} and value is None:
                continue
            merged[key] = value
        return merged

    def collect(self, now: float) -> dict[str, Any]:
        dashboard_payload, dashboard_error = _get_json(
            f"{self.args.api_url.rstrip('/')}/api/cameras",
            self.args.http_timeout,
        )
        dashboard_rows: dict[str, Any] = {}
        if dashboard_payload:
            for row in dashboard_payload.get("cameras", []):
                if isinstance(row, dict) and row.get("name"):
                    dashboard_rows[str(row["name"])] = row

        peer_interfaces = _link_local_interfaces()
        candidate_ips = sorted(
            set(DEFAULT_CAMERA_IPS.values()) | set(peer_interfaces)
        )
        include_metadata = now - self.last_metadata_poll >= self.args.metadata_interval
        if include_metadata:
            self.last_metadata_poll = now
        metadata_by_ip = {
            ip: include_metadata
            or _reported_camera_name(self.peer_metadata.get(ip, {})) is None
            for ip in candidate_ips
        }
        with ThreadPoolExecutor(max_workers=max(1, len(candidate_ips))) as executor:
            futures = {
                ip: executor.submit(
                    self._camera_probe,
                    ip,
                    metadata_by_ip[ip],
                )
                for ip in candidate_ips
            }
            fresh_probes = {ip: future.result() for ip, future in futures.items()}
        probes = {
            ip: self._merge_peer_metadata(ip, probe, metadata_by_ip[ip])
            for ip, probe in fresh_probes.items()
        }
        assignments, identity_conflicts = map_probes_by_camera_identity(
            self.camera_names,
            probes,
        )

        cameras: dict[str, Any] = {}
        for name in self.camera_names:
            assigned = assignments.get(name)
            if assigned is None:
                cameras[name] = {
                    "name": name,
                    "discovered": False,
                    "http_ok": False,
                    "http_error": "no probed peer reports this cameraNamespace",
                    "candidate_peers": {
                        ip: {
                            "http_ok": probe.get("http_ok"),
                            "reported_camera": _reported_camera_name(probe),
                        }
                        for ip, probe in probes.items()
                    },
                    "interface": _with_counter_deltas(
                        {"name": None, "present": False},
                        self.previous_interfaces.get(name),
                    ),
                }
                self.previous_interfaces[name] = dict(cameras[name]["interface"])
                continue

            ip = str(assigned.get("ip"))
            interface = _interface_snapshot(peer_interfaces.get(ip))
            interface = _with_counter_deltas(interface, self.previous_interfaces.get(name))
            self.previous_interfaces[name] = dict(interface)
            cameras[name] = {
                **assigned,
                "name": name,
                "discovered": True,
                "interface": interface,
            }
            previous_ip = self.previous_camera_ips.get(name)
            if previous_ip and previous_ip != ip:
                self.writer.emit(
                    "camera_peer_ip_changed",
                    severity="info",
                    cause="camera_network_address_reassigned",
                    camera=name,
                    previous_ip=previous_ip,
                    current_ip=ip,
                )
            self.previous_camera_ips[name] = ip

        runtime_pids = _runtime_pids()
        if self.previous_runtime_pids is not None and runtime_pids != self.previous_runtime_pids:
            self.writer.emit(
                "dashboard_runtime_process_changed",
                severity="warning",
                cause="dashboard_backend_exit_or_restart",
                previous_pids=self.previous_runtime_pids,
                current_pids=runtime_pids,
            )
        self.previous_runtime_pids = runtime_pids

        return {
            "expected_ros_domain_id": self.expected_domain,
            "dashboard": {
                "ok": dashboard_payload is not None,
                "error": dashboard_error,
                "runtime": dashboard_payload.get("runtime") if dashboard_payload else None,
                "cameras": dashboard_rows,
            },
            "cameras": cameras,
            "camera_identity_conflicts": identity_conflicts,
            "probed_peers": {
                ip: {
                    "http_ok": probe.get("http_ok"),
                    "reported_camera": _reported_camera_name(probe),
                    "interface": peer_interfaces.get(ip),
                }
                for ip, probe in probes.items()
            },
            "dashboard_runtime_pids": runtime_pids,
        }

    def run(self) -> int:
        self._start_kernel_journal()
        started = time.monotonic()
        self.writer.emit(
            "monitor_started",
            severity="info",
            pid=os.getpid(),
            config=str(Path(self.args.config).resolve()),
            api_url=self.args.api_url,
            camera_names=self.camera_names,
            interval_sec=self.args.interval,
            log_path=str(self.writer.path),
        )
        print(f"camera monitor log: {self.writer.path}", flush=True)
        try:
            while not self.stop_event.is_set():
                cycle_started = time.monotonic()
                try:
                    snapshot = self.collect(cycle_started)
                    conditions = derive_conditions(
                        snapshot,
                        low_fps_ratio=self.args.low_fps_ratio,
                    )
                    for state, code, condition, observed_sec in self.tracker.update(
                        conditions, cycle_started
                    ):
                        self.writer.emit(
                            "fault_started" if state == "started" else "fault_resolved",
                            severity=condition.severity if state == "started" else "info",
                            code=code,
                            cause=condition.cause,
                            summary=condition.summary,
                            observed_sec=round(observed_sec, 3),
                            evidence=dict(condition.evidence),
                        )
                    if cycle_started - self.last_snapshot_log >= self.args.snapshot_interval:
                        self.writer.emit(
                            "snapshot",
                            severity="info",
                            active_faults=sorted(self.tracker.active),
                            data=snapshot,
                        )
                        self.last_snapshot_log = cycle_started
                except Exception as exc:  # noqa: BLE001 - monitor must preserve later evidence
                    self.writer.emit(
                        "monitor_poll_error",
                        severity="warning",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                if self.args.duration > 0 and time.monotonic() - started >= self.args.duration:
                    break
                remaining = self.args.interval - (time.monotonic() - cycle_started)
                self.stop_event.wait(max(0.0, remaining))
        finally:
            self.stop_event.set()
            if self.kernel_process is not None and self.kernel_process.poll() is None:
                self.kernel_process.terminate()
                try:
                    self.kernel_process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self.kernel_process.kill()
            if self.kernel_thread is not None:
                self.kernel_thread.join(timeout=2.0)
            self.writer.emit(
                "monitor_stopped",
                severity="info",
                active_faults=sorted(self.tracker.active),
            )
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Continuously log evidence and causes for camera outages."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--api-url", default="http://127.0.0.1:8765")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--snapshot-interval", type=float, default=30.0)
    parser.add_argument("--metadata-interval", type=float, default=60.0)
    parser.add_argument("--http-timeout", type=float, default=0.7)
    parser.add_argument("--sustain-sec", type=float, default=3.0)
    parser.add_argument("--low-fps-ratio", type=float, default=0.8)
    parser.add_argument("--duration", type=float, default=0.0, help="0 means run until interrupted")
    parser.add_argument("--no-kernel-journal", action="store_true")
    parser.add_argument("--max-log-mb", type=float, default=100.0)
    parser.add_argument(
        "--pid-file",
        type=Path,
        help="default: <output-dir>/monitor.pid",
    )
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    for name in ("interval", "snapshot_interval", "metadata_interval", "http_timeout"):
        if getattr(args, name) <= 0:
            raise SystemExit(f"--{name.replace('_', '-')} must be positive")
    if args.sustain_sec < 0 or args.duration < 0:
        raise SystemExit("--sustain-sec and --duration cannot be negative")
    if args.max_log_mb <= 0:
        raise SystemExit("--max-log-mb must be positive")
    if not 0 < args.low_fps_ratio <= 1:
        raise SystemExit("--low-fps-ratio must be in (0, 1]")


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    _validate_args(args)
    lock = InstanceLock(args.pid_file or args.output_dir / "monitor.pid")
    stamp = dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    try:
        writer = JsonlWriter(
            args.output_dir / f"camera-monitor-{stamp}.jsonl",
            max_bytes=round(args.max_log_mb * 1024 * 1024),
        )
        monitor = CameraFailureMonitor(args, writer)
    except Exception:
        lock.close()
        raise

    def stop(_signum: int, _frame: object) -> None:
        monitor.stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        return monitor.run()
    finally:
        writer.close()
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
