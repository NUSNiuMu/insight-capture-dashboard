#!/usr/bin/env python3
"""Set one ROS domain ID on all cameras and the local dashboard runtime."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "cameras.json"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
DEVICE_CLI = PROJECT_ROOT / "tools" / "device_cli" / "looper_cli.py"
ROS_DOMAIN_ENDPOINT = "/api/ros-domain-id"
VERSION_ENDPOINT = "/api/version"
MAX_ROS_DOMAIN_ID = 232


class DomainUpdateError(RuntimeError):
    """A field-safe ROS domain update failure."""


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def validate_domain_id(value: str) -> int:
    try:
        domain_id = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("ROS domain ID must be an integer") from exc
    if not 0 <= domain_id <= MAX_ROS_DOMAIN_ID:
        raise argparse.ArgumentTypeError(
            f"ROS domain ID must be between 0 and {MAX_ROS_DOMAIN_ID}"
        )
    return domain_id


def parse_camera_urls(ip_output: str) -> list[str]:
    """Derive point-to-point camera peers from active link-local host addresses."""

    urls = set()
    for line in ip_output.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        interface = fields[1]
        if interface == "lo" or interface.startswith(("docker", "br-")):
            continue
        address = fields[3].split("/", 1)[0]
        octets = address.split(".")
        if len(octets) != 4 or octets[:2] != ["169", "254"]:
            continue
        peer_last = "2" if octets[3] == "1" else "1"
        urls.add(f"http://{'.'.join((*octets[:3], peer_last))}")
    return sorted(urls)


def discover_camera_urls() -> list[str]:
    completed = subprocess.run(
        ["ip", "-4", "-o", "addr", "show", "up"],
        check=True,
        capture_output=True,
        text=True,
    )
    return parse_camera_urls(completed.stdout)


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
    timeout: float = 8.0,
) -> object:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "insight-ros-domain-config/1.0",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        body = response.read()
    return json.loads(body.decode("utf-8")) if body else None


def read_camera_domain(url: str) -> int:
    payload = request_json(f"{url.rstrip('/')}{ROS_DOMAIN_ENDPOINT}")
    if not isinstance(payload, dict) or not payload.get("success"):
        raise DomainUpdateError(f"{url}: failed to read ROS domain ID")
    data = payload.get("data")
    if not isinstance(data, dict) or "rosDomainId" not in data:
        raise DomainUpdateError(f"{url}: ROS domain response has no rosDomainId")
    try:
        return int(data["rosDomainId"])
    except (TypeError, ValueError) as exc:
        raise DomainUpdateError(
            f"{url}: invalid ROS domain ID {data['rosDomainId']!r}"
        ) from exc


def set_camera_domain(url: str, domain_id: int) -> None:
    payload = request_json(
        f"{url.rstrip('/')}{ROS_DOMAIN_ENDPOINT}",
        method="POST",
        payload={"rosDomainId": str(domain_id)},
        timeout=30.0,
    )
    if not isinstance(payload, dict) or not payload.get("success"):
        message = payload.get("message") if isinstance(payload, dict) else payload
        raise DomainUpdateError(f"{url}: ROS domain update failed: {message}")
    actual = read_camera_domain(url)
    if actual != domain_id:
        raise DomainUpdateError(
            f"{url}: verification returned {actual}, expected {domain_id}"
        )


def enabled_camera_count(config_path: Path) -> int:
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DomainUpdateError(f"cannot read {config_path}: {exc}") from exc
    cameras = payload.get("cameras") if isinstance(payload, dict) else None
    if not isinstance(cameras, list):
        raise DomainUpdateError(f"{config_path} has no cameras list")
    return sum(
        1
        for camera in cameras
        if isinstance(camera, dict) and camera.get("enabled", True)
    )


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        temporary.write(content)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    temporary_path.chmod(mode)
    os.replace(temporary_path, path)


def update_camera_config(config_path: Path, domain_id: int) -> None:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise DomainUpdateError(f"{config_path} must contain a JSON object")
    payload["ros_domain_id"] = domain_id
    atomic_write(
        config_path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def update_env_file(env_path: Path, domain_id: int) -> None:
    lines = (
        env_path.read_text(encoding="utf-8").splitlines()
        if env_path.exists()
        else []
    )
    updated = []
    replaced = False
    for line in lines:
        if line.startswith("ROS_DOMAIN_ID="):
            if not replaced:
                updated.append(f"ROS_DOMAIN_ID={domain_id}")
                replaced = True
            continue
        updated.append(line)
    if not replaced:
        updated.append(f"ROS_DOMAIN_ID={domain_id}")
    atomic_write(env_path, "\n".join(updated) + "\n")


def recording_is_active() -> bool:
    try:
        payload = request_json(
            "http://127.0.0.1:8765/api/recording/status",
            timeout=3.0,
        )
    except (HTTPError, URLError, OSError, TimeoutError, json.JSONDecodeError):
        log(
            "Dashboard status is unavailable; continuing because no active "
            "recording was detected."
        )
        return False
    return isinstance(payload, dict) and bool(payload.get("recording"))


def reboot_camera(url: str) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(DEVICE_CLI),
            "--device-base-url",
            url,
            "system",
            "reboot",
            "-y",
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise DomainUpdateError(
            f"{url}: reboot failed: {detail[-1] if detail else completed.returncode}"
        )


def wait_for_camera(url: str, timeout_sec: float) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            payload = request_json(f"{url.rstrip('/')}{VERSION_ENDPOINT}", timeout=2.0)
            if isinstance(payload, dict) and payload.get("version"):
                return
        except (HTTPError, URLError, OSError, TimeoutError, json.JSONDecodeError):
            pass
        time.sleep(2.0)
    raise DomainUpdateError(f"{url}: did not return after {timeout_sec:g}s")


def run_parallel(urls: Iterable[str], action, description: str) -> None:
    failures = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(action, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                future.result()
                log(f"{description}: {url}")
            except Exception as exc:  # noqa: BLE001 - aggregate all camera failures
                failures.append(str(exc))
    if failures:
        raise DomainUpdateError("; ".join(failures))


def recreate_runtime(compose_file: Path) -> None:
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "up",
            "-d",
            "--force-recreate",
            "insight9-sparse-mapper",
            "insight3-global-localizer",
            "insight-dashboard",
        ],
        cwd=compose_file.parent,
        check=False,
    )
    if completed.returncode != 0:
        raise DomainUpdateError(
            "camera and config updates succeeded, but Docker services were not recreated"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Set the same ROS domain ID on all configured cameras, "
            "config/cameras.json, and the Compose .env file."
        )
    )
    parser.add_argument("domain_id", type=validate_domain_id)
    parser.add_argument(
        "--camera-url",
        action="append",
        default=[],
        help="explicit camera base URL; repeat for each camera",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_PATH)
    parser.add_argument(
        "--compose-file",
        type=Path,
        default=PROJECT_ROOT / "docker-compose.yml",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="write settings without rebooting cameras or recreating Docker services",
    )
    parser.add_argument("-y", "--yes", action="store_true")
    parser.add_argument("--camera-wait-timeout", type=float, default=120.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = args.config.resolve()
    env_path = args.env_file.resolve()
    compose_file = args.compose_file.resolve()
    urls = sorted({url.rstrip("/") for url in args.camera_url})
    if not urls:
        try:
            urls = discover_camera_urls()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise DomainUpdateError(f"camera discovery failed: {exc}") from exc

    expected = enabled_camera_count(config_path)
    if len(urls) != expected:
        raise DomainUpdateError(
            f"found {len(urls)} camera(s), but {config_path} enables {expected}: "
            f"{', '.join(urls) or 'none'}"
        )
    if expected != 3:
        raise DomainUpdateError(
            f"this fleet operation requires exactly three enabled cameras, found {expected}"
        )

    current = {}
    for url in urls:
        current[url] = read_camera_domain(url)
        log(f"Camera {url}: ROS domain {current[url]} -> {args.domain_id}")
    log(f"Runtime config: {config_path}")
    log(f"Compose environment: {env_path}")

    if args.dry_run:
        log("Dry run complete; no settings were changed.")
        return 0
    if recording_is_active():
        raise DomainUpdateError("recording is active; stop it before changing ROS domain")
    if not args.yes:
        answer = input(
            "Update all three cameras and the local runtime"
            + (" without restarting" if args.no_restart else ", then restart them")
            + "? [y/N]: "
        ).strip().lower()
        if answer not in {"y", "yes"}:
            log("Aborted; no settings were changed.")
            return 1

    config_before = config_path.read_text(encoding="utf-8")
    env_before = env_path.read_text(encoding="utf-8") if env_path.exists() else None
    changed_urls = []
    try:
        for url in urls:
            if current[url] == args.domain_id:
                continue
            set_camera_domain(url, args.domain_id)
            changed_urls.append(url)
            log(f"Updated camera {url} to ROS domain {args.domain_id}")
        update_camera_config(config_path, args.domain_id)
        update_env_file(env_path, args.domain_id)
        log(f"Updated local runtime to ROS domain {args.domain_id}")
    except Exception as exc:
        log(f"Update failed; attempting rollback: {exc}")
        for url in reversed(changed_urls):
            try:
                set_camera_domain(url, current[url])
                log(f"Rolled back {url} to ROS domain {current[url]}")
            except Exception as rollback_exc:  # noqa: BLE001 - report partial rollback
                log(f"WARNING: rollback failed for {url}: {rollback_exc}")
        atomic_write(config_path, config_before)
        if env_before is None:
            env_path.unlink(missing_ok=True)
        else:
            atomic_write(env_path, env_before)
        raise DomainUpdateError(str(exc)) from exc

    if args.no_restart:
        log("Settings saved. Reboot cameras and recreate Docker services before using ROS.")
        return 0

    run_parallel(urls, reboot_camera, "Reboot requested")
    log("Waiting 10s for camera shutdown to begin...")
    time.sleep(10.0)
    run_parallel(
        urls,
        lambda url: wait_for_camera(url, args.camera_wait_timeout),
        "Camera online",
    )
    for url in urls:
        actual = read_camera_domain(url)
        if actual != args.domain_id:
            raise DomainUpdateError(
                f"{url}: rebooted with ROS domain {actual}, expected {args.domain_id}"
            )
        log(f"Verified camera {url}: ROS domain {actual}")
    recreate_runtime(compose_file)
    log(f"ROS domain {args.domain_id} is active on all cameras and local services.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DomainUpdateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
