#!/usr/bin/env python3
"""Synchronize camera clocks, restart capture together, and measure timestamp skew."""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import getpass
import json
import math
import os
from pathlib import Path
import re
import shlex
import statistics
import subprocess
import sys
import time
from typing import Callable, TypeVar
from urllib.error import URLError
from urllib.request import urlopen


NTP_OFFSET_RE = re.compile(r"offset\s+([+-]?\d+(?:\.\d+)?)")
LPWM_EVENT_RE = re.compile(
    r"\[(\d+(?:\.\d+)?)\].*\bVIN lpwm_enable=(\d+)\b"
)


MEASUREMENT_WORKER = r'''
import bisect
import itertools
import json
import os
import statistics
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import CompressedImage, Image


STREAM_CONFIG = json.loads(os.environ["CAMERA_MEASURE_STREAMS"])
MESSAGE_TYPES = {"image": Image, "compressed": CompressedImage}
STREAMS = {
    name: (MESSAGE_TYPES[spec["type"]], spec["topic"])
    for name, spec in STREAM_CONFIG.items()
}
SAMPLES = {name: [] for name in STREAMS}


class StampCollector(Node):
    def __init__(self):
        super().__init__("camera_restart_stamp_measurement")
        qos = QoSProfile(
            depth=100,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
        )
        self._camera_subscriptions = []
        for name, (message_type, topic) in STREAMS.items():
            self._camera_subscriptions.append(
                self.create_subscription(
                    message_type,
                    topic,
                    lambda message, key=name: SAMPLES[key].append(
                        message.header.stamp.sec
                        + message.header.stamp.nanosec * 1e-9
                    ),
                    qos,
                )
            )


def percentile(values, fraction):
    index = min(len(values) - 1, int(fraction * len(values)))
    return values[index]


def compare(reference_name, target_name):
    reference = SAMPLES[reference_name]
    target = SAMPLES[target_name]
    signed = []
    elapsed = []
    if reference:
        origin = reference[0]
        for value in target:
            if value < reference[0] or value > reference[-1]:
                continue
            position = bisect.bisect_left(reference, value)
            candidates = []
            if position < len(reference):
                candidates.append(reference[position])
            if position:
                candidates.append(reference[position - 1])
            nearest = min(candidates, key=lambda candidate: abs(value - candidate))
            signed.append((value - nearest) * 1000.0)
            elapsed.append(value - origin)
    if not signed:
        raise RuntimeError(f"No overlapping samples for {reference_name}/{target_name}")

    absolute = sorted(abs(value) for value in signed)
    slope = None
    if len(signed) >= 2:
        elapsed_mean = statistics.fmean(elapsed)
        signed_mean = statistics.fmean(signed)
        denominator = sum((value - elapsed_mean) ** 2 for value in elapsed)
        if denominator:
            slope = sum(
                (x - elapsed_mean) * (y - signed_mean)
                for x, y in zip(elapsed, signed)
            ) / denominator
    return {
        "reference": reference_name,
        "target": target_name,
        "matched": len(signed),
        "signed_median_ms": statistics.median(signed),
        "signed_min_ms": min(signed),
        "signed_max_ms": max(signed),
        "abs_median_ms": statistics.median(absolute),
        "abs_p95_ms": percentile(absolute, 0.95),
        "abs_max_ms": max(absolute),
        "drift_ms_per_sec": slope,
    }


duration = float(os.environ["CAMERA_MEASURE_SECONDS"])
rclpy.init()
node = StampCollector()
deadline = time.monotonic() + duration
while time.monotonic() < deadline:
    rclpy.spin_once(node, timeout_sec=0.1)
node.destroy_node()
rclpy.shutdown()

streams = {}
for name, values in SAMPLES.items():
    values.sort()
    periods = [(right - left) * 1000.0 for left, right in zip(values, values[1:])]
    streams[name] = {
        "count": len(values),
        "period_median_ms": statistics.median(periods) if periods else None,
    }

result = {
    "duration_sec": duration,
    "streams": streams,
    "pairs": [compare(left, right) for left, right in itertools.combinations(STREAMS, 2)],
}
print("CAMERA_RESTART_MEASUREMENT=" + json.dumps(result, sort_keys=True))
'''


class CameraSyncError(RuntimeError):
    """Raised when a synchronization or measurement step cannot complete."""


@dataclasses.dataclass(frozen=True)
class CameraSpec:
    name: str
    host: str
    ntp_server: str


@dataclasses.dataclass
class RemoteCamera:
    spec: CameraSpec
    client: object

    def run(self, command: str, *, timeout: float = 30.0) -> str:
        stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        del stdin
        status = stdout.channel.recv_exit_status()
        output = stdout.read().decode("utf-8", errors="replace")
        error = stderr.read().decode("utf-8", errors="replace")
        if status != 0:
            detail = (error or output).strip()
            raise CameraSyncError(
                f"{self.spec.name} command failed ({status}): {detail or command}"
            )
        return output + error

    def close(self) -> None:
        self.client.close()


T = TypeVar("T")


def run_parallel(
    cameras: list[RemoteCamera], operation: Callable[[RemoteCamera], T]
) -> dict[str, T]:
    results: dict[str, T] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(cameras)) as executor:
        futures = {executor.submit(operation, camera): camera for camera in cameras}
        for future in concurrent.futures.as_completed(futures):
            camera = futures[future]
            try:
                results[camera.spec.name] = future.result()
            except Exception as exc:
                raise CameraSyncError(f"{camera.spec.name}: {exc}") from exc
    return results


def connect_camera(
    spec: CameraSpec,
    *,
    username: str,
    password: str | None,
    identity_file: str | None,
    strict_host_keys: bool,
) -> RemoteCamera:
    try:
        import paramiko
    except ImportError as exc:
        raise CameraSyncError(
            "paramiko is required on the Jetson host (python3-paramiko)"
        ) from exc

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    if strict_host_keys:
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            spec.host,
            username=username,
            password=password,
            key_filename=identity_file,
            timeout=10,
            banner_timeout=10,
            auth_timeout=10,
            allow_agent=identity_file is not None,
            look_for_keys=identity_file is not None,
        )
    except Exception as exc:
        client.close()
        raise CameraSyncError(f"Cannot SSH to {spec.name} ({spec.host}): {exc}") from exc
    return RemoteCamera(spec=spec, client=client)


def parse_ntp_offset(output: str) -> float:
    match = NTP_OFFSET_RE.search(output)
    if not match:
        raise CameraSyncError(f"Unable to parse ntpdate output: {output.strip()}")
    return float(match.group(1)) * 1000.0


def query_clock_offset(camera: RemoteCamera) -> float:
    output = camera.run(
        f"ntpdate -q {shlex.quote(camera.spec.ntp_server)}", timeout=20
    )
    return parse_ntp_offset(output)


def synchronize_clock(camera: RemoteCamera) -> float:
    camera.run(f"ntpdate -b {shlex.quote(camera.spec.ntp_server)}", timeout=20)
    return query_clock_offset(camera)


def ensure_recording_idle(status_url: str, *, force: bool) -> None:
    try:
        with urlopen(status_url, timeout=3) as response:
            status = json.load(response)
    except (OSError, URLError, ValueError) as exc:
        if force:
            print(f"WARNING: recording status unavailable: {exc}", file=sys.stderr)
            return
        raise CameraSyncError(
            f"Cannot verify recording state at {status_url}; use --force to override"
        ) from exc
    if status.get("recording") and not force:
        raise CameraSyncError("Dashboard is recording; stop it or use --force")


def schedule_restart(
    camera: RemoteCamera, *, unit: str, calendar_time: str
) -> str:
    command = (
        f"systemd-run --unit={shlex.quote(unit)} "
        f"--on-calendar={shlex.quote(calendar_time)} "
        "--timer-property=AccuracySec=1ms "
        "--timer-property=RandomizedDelaySec=0 "
        "/bin/systemctl restart S99all_run.service"
    )
    output = camera.run(command)
    camera.run(f"systemctl is-active {shlex.quote(unit)}.timer")
    return output.strip()


def cancel_restart(camera: RemoteCamera, *, unit: str) -> None:
    try:
        camera.run(f"systemctl stop {shlex.quote(unit)}.timer", timeout=10)
    except Exception:
        pass


def collect_restart_details(
    camera: RemoteCamera, *, unit: str, target_epoch: int
) -> dict[str, object]:
    active = camera.run("systemctl is-active S99all_run.service").strip()
    if active != "active":
        raise CameraSyncError(f"{camera.spec.name} service is {active}")

    journal = camera.run(
        f"journalctl -u {shlex.quote(unit)}.service "
        f"--since=@{target_epoch - 2} --until=@{target_epoch + 60} "
        "-o json --no-pager",
        timeout=20,
    )
    trigger_us = None
    for line in journal.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = str(entry.get("MESSAGE", ""))
        if message.startswith("Started /bin/systemctl restart S99all_run.service"):
            trigger_us = int(entry["__REALTIME_TIMESTAMP"])
            break
    if trigger_us is None:
        raise CameraSyncError(f"No timer trigger journal found for {camera.spec.name}")

    log_output = camera.run(
        "latest=$(ls -t /root/.ros/log/insight_full_*.log | head -n 1); "
        "printf 'LOG=%s\\n' \"$latest\"; "
        "grep -E 'NTP sync OK|VIN lpwm_enable=' \"$latest\" || true",
        timeout=20,
    )
    lines = log_output.splitlines()
    log_path = lines[0].removeprefix("LOG=") if lines else ""
    lpwm_events = [
        {"timestamp": float(match.group(1)), "enabled": bool(int(match.group(2)))}
        for line in lines[1:]
        if (match := LPWM_EVENT_RE.search(line))
    ]
    return {
        "host": camera.spec.host,
        "service_active": True,
        "timer_trigger_epoch": trigger_us / 1_000_000.0,
        "log_path": log_path,
        "lpwm_events": lpwm_events,
    }


def measure_image_timestamps(
    *,
    container: str,
    ros_domain_id: int,
    duration_sec: float,
    streams: dict[str, dict[str, str]],
) -> dict[str, object]:
    command = [
        "docker",
        "exec",
        "-i",
        "-e",
        f"ROS_DOMAIN_ID={ros_domain_id}",
        "-e",
        "ROS_LOG_DIR=/tmp/camera_restart_measurement",
        "-e",
        f"CAMERA_MEASURE_SECONDS={duration_sec}",
        "-e",
        f"CAMERA_MEASURE_STREAMS={json.dumps(streams, separators=(',', ':'))}",
        container,
        "bash",
        "-lc",
        "source /opt/ros/humble/setup.bash 2>/dev/null || true; "
        "source /userdata/hobot/opt/ros/humble/setup.bash 2>/dev/null || true; "
        "python3 -",
    ]
    try:
        process = subprocess.run(
            command,
            input=MEASUREMENT_WORKER,
            text=True,
            capture_output=True,
            timeout=duration_sec + 30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CameraSyncError(f"Timestamp measurement failed: {exc}") from exc
    if process.returncode != 0:
        raise CameraSyncError(
            f"Timestamp measurement failed: {(process.stderr or process.stdout).strip()}"
        )
    prefix = "CAMERA_RESTART_MEASUREMENT="
    for line in process.stdout.splitlines():
        if line.startswith(prefix):
            return json.loads(line[len(prefix) :])
    raise CameraSyncError(f"Measurement result missing: {process.stdout.strip()}")


def load_ros_domain_id(config_path: Path) -> int:
    try:
        with config_path.open(encoding="utf-8") as stream:
            return int(json.load(stream).get("ros_domain_id", 20))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 20


def load_camera_specs(
    config_path: Path,
) -> tuple[list[CameraSpec], dict[str, dict[str, str]]]:
    """Build SSH and timestamp-measurement inputs from the active profile."""

    with config_path.open(encoding="utf-8") as stream:
        payload = json.load(stream)
    enabled = [
        camera
        for camera in payload.get("cameras", [])
        if camera.get("enabled", True)
    ]
    if len(enabled) < 2:
        raise CameraSyncError("camera config must contain at least two enabled cameras")
    specs = []
    streams = {}
    for index, camera in enumerate(enabled, start=1):
        name = str(camera["name"])
        namespace = str(camera["namespace"])
        subnet = index * 10
        specs.append(
            CameraSpec(
                name=name,
                host=str(camera.get("device_ip", f"169.254.{subnet}.1")),
                ntp_server=str(camera.get("ntp_server", f"169.254.{subnet}.2")),
            )
        )
        image_stream = str(
            camera.get("sync_image_stream", camera.get("dashboard_image_stream", "color_compressed"))
        )
        if image_stream == "color_compressed":
            streams[name] = {
                "type": "compressed",
                "topic": f"/{namespace}/camera/color/image_rect_raw/compressed",
            }
        elif image_stream in {"infra1", "infra2", "color", "depth"}:
            streams[name] = {
                "type": "image",
                "topic": f"/{namespace}/camera/{image_stream}/image_rect_raw",
            }
        else:
            raise CameraSyncError(
                f"unsupported sync image stream '{image_stream}' for {name}"
            )
    return specs, streams


def print_clock_offsets(title: str, offsets: dict[str, float]) -> None:
    print(title)
    for name in sorted(offsets):
        print(f"  {name:<12} {offsets[name]:+8.3f} ms")
    spread = max(offsets.values()) - min(offsets.values())
    print(f"  {'estimated spread':<12} {spread:8.3f} ms")


def print_report(report: dict[str, object], *, tolerance_ms: float) -> None:
    details = report["restart_details"]
    triggers = {
        name: float(value["timer_trigger_epoch"]) for name, value in details.items()
    }
    trigger_spread_ms = (max(triggers.values()) - min(triggers.values())) * 1000.0
    print("\nRestart timer results")
    for name in sorted(triggers):
        timestamp = dt.datetime.fromtimestamp(triggers[name]).astimezone()
        print(f"  {name:<12} {timestamp.strftime('%H:%M:%S.%f')}")
    print(f"  {'trigger spread':<12} {trigger_spread_ms:8.3f} ms")

    print("\nLPWM configuration events")
    for name in sorted(details):
        events = details[name]["lpwm_events"]
        rendered = ", ".join(
            f"{dt.datetime.fromtimestamp(event['timestamp']).strftime('%H:%M:%S.%f')}="
            f"{'on' if event['enabled'] else 'off'}"
            for event in events
        )
        print(f"  {name:<12} {rendered or 'none'}")

    measurement = report["measurement"]
    print("\nImage timestamp measurement")
    for name, stream in measurement["streams"].items():
        period = stream["period_median_ms"]
        print(
            f"  {name:<12} count={stream['count']:<4} "
            f"period={period:.3f} ms"
        )
    print(
        "\n  Pair                           median       range                "
        "abs p95    abs max    drift"
    )
    print("  " + "-" * 88)
    for pair in measurement["pairs"]:
        label = f"{pair['target']} - {pair['reference']}"
        drift = pair["drift_ms_per_sec"]
        print(
            f"  {label:<30} {pair['signed_median_ms']:+8.3f} ms  "
            f"[{pair['signed_min_ms']:+7.3f},{pair['signed_max_ms']:+7.3f}]  "
            f"{pair['abs_p95_ms']:8.3f}  {pair['abs_max_ms']:8.3f}  "
            f"{drift:+7.3f} ms/s"
        )
    maximum = max(float(pair["abs_max_ms"]) for pair in measurement["pairs"])
    verdict = "PASS" if maximum <= tolerance_ms else "FAIL"
    print(
        f"\nResult: {verdict} (max observed skew {maximum:.3f} ms, "
        f"limit {tolerance_ms:.3f} ms)"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-user", default="root")
    parser.add_argument(
        "--password-env",
        default="INSIGHT_CAMERA_SSH_PASSWORD",
        help="Environment variable containing the SSH password",
    )
    parser.add_argument("--identity-file", help="SSH private key instead of a password")
    parser.add_argument("--strict-host-keys", action="store_true")
    parser.add_argument("--lead-seconds", type=int, default=30)
    parser.add_argument("--recovery-seconds", type=float, default=15.0)
    parser.add_argument("--measure-seconds", type=float, default=10.0)
    parser.add_argument("--tolerance-ms", type=float, default=10.0)
    parser.add_argument("--container", default="insight-dashboard")
    parser.add_argument(
        "--recording-status-url",
        default="http://127.0.0.1:8765/api/recording/status",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only check SSH connectivity and current NTP offsets",
    )
    parser.add_argument("--json", action="store_true", help="Also print the raw report")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.lead_seconds < 10:
        raise CameraSyncError("--lead-seconds must be at least 10")
    if args.measure_seconds <= 0 or args.recovery_seconds < 0:
        raise CameraSyncError("Measurement and recovery durations must be positive")

    root = Path(__file__).resolve().parents[1]
    camera_config_path = root / "config" / "cameras.json"
    ros_domain_id = load_ros_domain_id(camera_config_path)
    specs, measurement_streams = load_camera_specs(camera_config_path)
    password = None
    if not args.identity_file:
        password = os.environ.get(args.password_env)
        if password is None:
            try:
                password = getpass.getpass("Camera SSH password: ")
            except EOFError as exc:
                raise CameraSyncError(
                    f"No interactive terminal; set {args.password_env} or --identity-file"
                ) from exc

    cameras: list[RemoteCamera] = []
    scheduled = False
    unit = ""
    try:
        print("Connecting to cameras...")
        for spec in specs:
            cameras.append(
                connect_camera(
                    spec,
                    username=args.ssh_user,
                    password=password,
                    identity_file=args.identity_file,
                    strict_host_keys=args.strict_host_keys,
                )
            )

        initial_offsets = run_parallel(cameras, query_clock_offset)
        print_clock_offsets("Current NTP offsets", initial_offsets)
        if args.check_only:
            return 0

        ensure_recording_idle(args.recording_status_url, force=args.force)
        print("\nSynchronizing camera clocks...")
        synchronized_offsets = run_parallel(cameras, synchronize_clock)
        print_clock_offsets("Post-sync NTP offsets", synchronized_offsets)

        target_epoch = math.ceil(time.time()) + args.lead_seconds
        target_time = dt.datetime.fromtimestamp(target_epoch).astimezone()
        calendar_time = target_time.strftime("%Y-%m-%d %H:%M:%S")
        unit = f"camera-phase-restart-{target_epoch}"
        print(f"\nScheduling all cameras for {target_time.isoformat()}")
        # Mark the timer as potentially installed before the parallel call so
        # a partial scheduling failure cancels every camera that did succeed.
        scheduled = True
        run_parallel(
            cameras,
            lambda camera: schedule_restart(
                camera, unit=unit, calendar_time=calendar_time
            ),
        )

        wait_seconds = max(0.0, target_epoch - time.time()) + args.recovery_seconds
        print(f"Waiting {wait_seconds:.1f}s for restart and recovery...")
        time.sleep(wait_seconds)

        restart_details = run_parallel(
            cameras,
            lambda camera: collect_restart_details(
                camera, unit=unit, target_epoch=target_epoch
            ),
        )
        final_offsets = run_parallel(cameras, query_clock_offset)
        measurement = measure_image_timestamps(
            container=args.container,
            ros_domain_id=ros_domain_id,
            duration_sec=args.measure_seconds,
            streams=measurement_streams,
        )
        report = {
            "target_epoch": target_epoch,
            "target_time": target_time.isoformat(),
            "initial_ntp_offset_ms": initial_offsets,
            "post_sync_ntp_offset_ms": synchronized_offsets,
            "final_ntp_offset_ms": final_offsets,
            "restart_details": restart_details,
            "measurement": measurement,
        }
        print_clock_offsets("\nFinal NTP offsets", final_offsets)
        print_report(report, tolerance_ms=args.tolerance_ms)
        if args.json:
            print("\n" + json.dumps(report, indent=2, sort_keys=True))
        maximum = max(
            float(pair["abs_max_ms"]) for pair in measurement["pairs"]
        )
        return 0 if maximum <= args.tolerance_ms else 2
    except (CameraSyncError, OSError) as exc:
        if scheduled and unit and time.time() < target_epoch:
            for camera in cameras:
                cancel_restart(camera, unit=unit)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        for camera in cameras:
            camera.close()


if __name__ == "__main__":
    try:
        exit_code = main()
    except CameraSyncError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        exit_code = 1
    raise SystemExit(exit_code)
