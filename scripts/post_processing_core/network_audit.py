"""Capture kernel receive-path counters around a recording session."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, List


_INTERFACE_COUNTERS = (
    "rx_dropped",
    "rx_errors",
    "rx_fifo_errors",
    "rx_missed_errors",
)


def _read_int(path: Path) -> int:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return 0


def _read_proc_tables(path: Path) -> Dict[str, Dict[str, int]]:
    try:
        lines = [line.split() for line in path.read_text().splitlines() if line.strip()]
    except OSError:
        return {}
    tables: Dict[str, Dict[str, int]] = {}
    for header, values in zip(lines, lines[1:]):
        if not header or not values or header[0] != values[0]:
            continue
        name = header[0].rstrip(":")
        try:
            tables[name] = {
                key: int(value) for key, value in zip(header[1:], values[1:])
            }
        except ValueError:
            continue
    return tables


def _camera_interfaces(sys_root: Path) -> Dict[str, Dict[str, object]]:
    interfaces: Dict[str, Dict[str, object]] = {}
    net_root = sys_root / "class" / "net"
    for path in sorted(net_root.glob("enx*")):
        try:
            driver = (path / "device" / "driver").resolve().name
        except OSError:
            driver = ""
        if driver and driver != "cdc_ncm":
            continue
        counters = {
            name: _read_int(path / "statistics" / name)
            for name in _INTERFACE_COUNTERS
        }
        rps_cpus: Dict[str, str] = {}
        for queue_path in sorted((path / "queues").glob("rx-*")):
            try:
                rps_cpus[queue_path.name] = (queue_path / "rps_cpus").read_text().strip()
            except OSError:
                continue
        interfaces[path.name] = {"counters": counters, "rps_cpus": rps_cpus}
    return interfaces


def capture_network_snapshot(
    proc_root: Path = Path("/proc"), sys_root: Path = Path("/sys")
) -> Dict[str, object]:
    """Return monotonic receive counters without invoking external commands."""

    softnet_dropped: List[int] = []
    softnet_time_squeeze: List[int] = []
    try:
        for line in (proc_root / "net" / "softnet_stat").read_text().splitlines():
            fields = line.split()
            if len(fields) < 3:
                continue
            softnet_dropped.append(int(fields[1], 16))
            softnet_time_squeeze.append(int(fields[2], 16))
    except (OSError, ValueError):
        pass

    snmp = _read_proc_tables(proc_root / "net" / "snmp")
    ip = snmp.get("Ip", {})
    udp = snmp.get("Udp", {})
    return {
        "captured_at_epoch_s": time.time(),
        "settings": {
            "net.ipv4.ipfrag_max_dist": _read_int(
                proc_root / "sys" / "net" / "ipv4" / "ipfrag_max_dist"
            ),
        },
        "softnet_dropped_by_cpu": softnet_dropped,
        "softnet_time_squeeze_by_cpu": softnet_time_squeeze,
        "ip": {
            "reasm_fails": int(ip.get("ReasmFails", 0)),
            "reasm_timeouts": int(ip.get("ReasmTimeout", 0)),
        },
        "udp": {
            "in_errors": int(udp.get("InErrors", 0)),
            "rcvbuf_errors": int(udp.get("RcvbufErrors", 0)),
        },
        "interfaces": _camera_interfaces(sys_root),
    }


def _delta(start: int, end: int) -> int:
    return max(0, int(end) - int(start))


def _softnet_delta(start: List[int], end: List[int]) -> int:
    count = min(len(start), len(end))
    # softnet_stat fields are 32-bit counters and may wrap during long runs.
    return sum((int(end[index]) - int(start[index])) & 0xFFFFFFFF for index in range(count))


def compare_network_snapshots(
    start: Dict[str, object], end: Dict[str, object]
) -> Dict[str, object]:
    """Build a loss-focused delta report for a completed recording."""

    start_ip = dict(start.get("ip") or {})
    end_ip = dict(end.get("ip") or {})
    start_udp = dict(start.get("udp") or {})
    end_udp = dict(end.get("udp") or {})
    start_interfaces = dict(start.get("interfaces") or {})
    end_interfaces = dict(end.get("interfaces") or {})
    start_settings = dict(start.get("settings") or {})
    end_settings = dict(end.get("settings") or {})

    interfaces: Dict[str, Dict[str, object]] = {}
    for name in sorted(set(start_interfaces) | set(end_interfaces)):
        start_item = dict(start_interfaces.get(name) or {})
        end_item = dict(end_interfaces.get(name) or {})
        start_counters = dict(start_item.get("counters") or {})
        end_counters = dict(end_item.get("counters") or {})
        interfaces[name] = {
            "counter_deltas": {
                counter: _delta(start_counters.get(counter, 0), end_counters.get(counter, 0))
                for counter in _INTERFACE_COUNTERS
            },
            "rps_cpus": dict(end_item.get("rps_cpus") or {}),
        }

    deltas = {
        "softnet_dropped": _softnet_delta(
            list(start.get("softnet_dropped_by_cpu") or []),
            list(end.get("softnet_dropped_by_cpu") or []),
        ),
        "softnet_time_squeeze": _softnet_delta(
            list(start.get("softnet_time_squeeze_by_cpu") or []),
            list(end.get("softnet_time_squeeze_by_cpu") or []),
        ),
        "ip_reasm_fails": _delta(
            start_ip.get("reasm_fails", 0), end_ip.get("reasm_fails", 0)
        ),
        "ip_reasm_timeouts": _delta(
            start_ip.get("reasm_timeouts", 0), end_ip.get("reasm_timeouts", 0)
        ),
        "udp_in_errors": _delta(start_udp.get("in_errors", 0), end_udp.get("in_errors", 0)),
        "udp_rcvbuf_errors": _delta(
            start_udp.get("rcvbuf_errors", 0), end_udp.get("rcvbuf_errors", 0)
        ),
    }
    interface_rx_dropped = sum(
        int(item["counter_deltas"]["rx_dropped"]) for item in interfaces.values()
    )
    # time_squeeze means the NAPI budget was exhausted, not that a packet was
    # lost. Preserve it as headroom telemetry without failing the recording.
    loss_deltas = {
        name: value for name, value in deltas.items()
        if name != "softnet_time_squeeze" and int(value) > 0
    }
    issues = {
        **loss_deltas,
        **({"interface_rx_dropped": interface_rx_dropped} if interface_rx_dropped else {}),
    }
    settings = {
        name: {
            "start": int(start_settings.get(name, 0)),
            "end": int(end_settings.get(name, 0)),
        }
        for name in sorted(set(start_settings) | set(end_settings))
    }
    changed_settings = {
        name: values for name, values in settings.items()
        if values["start"] != values["end"]
    }
    if changed_settings:
        issues["settings_changed"] = changed_settings
    return {
        "method": "kernel_receive_counter_delta",
        "ok": not issues,
        "duration_s": round(
            max(
                0.0,
                float(end.get("captured_at_epoch_s", 0.0))
                - float(start.get("captured_at_epoch_s", 0.0)),
            ),
            3,
        ),
        "deltas": deltas,
        "settings": settings,
        "interfaces": interfaces,
        "issues": issues,
    }


def format_network_audit(audit: Dict[str, object]) -> str:
    issues = dict(audit.get("issues") or {})
    if not issues:
        return "kernel receive audit PASS -- no softnet, netdev, IP reassembly, or UDP drops"
    details = ", ".join(f"{name}={value}" for name, value in sorted(issues.items()))
    return f"kernel receive audit FAIL -- {details}"
