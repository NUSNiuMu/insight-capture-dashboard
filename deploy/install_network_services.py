#!/usr/bin/env python3
"""Install project-owned host network services without replacing site Wi-Fi settings."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile


def command(args, *, check=True):
    return subprocess.run(args, check=check, text=True, capture_output=True, timeout=60)


def install(source: Path, root=Path("/"), *, hotspot=False, defer_start=False,
            run=command, which=shutil.which):
    """root/run/which permit filesystem and service-boundary tests without sudo."""
    def target(path):
        return root / path.lstrip("/")

    marker = target("/etc/insight-capture-hotspot.conf")
    hotspot = hotspot or marker.exists()
    required = ["python3", "systemctl", "chronyd", "chronyc", "nmcli"]
    if hotspot:
        required += ["bash", "ip", "iw", "wpa_cli", "dnsmasq", "iptables"]
    missing = [name for name in required if which(name) is None]
    if missing:
        raise RuntimeError("Missing host network tools: " + ", ".join(missing))
    if hotspot:
        if not marker.is_file():
            raise RuntimeError("Initialize the capture hotspot UPLINK configuration first")
        # Query only; never read a secret or rewrite the site's connection.
        run(["nmcli", "-g", "connection.uuid", "connection", "show", "jetson-capture-hotspot"])

    # Validate the complete selected source set before replacing any host file.
    run(["chronyd", "-p", "-f", str(source / "hotspot/chrony.conf")])
    discovery = source.parent / "insight_capture/runtime/discovery.py"
    compile(discovery.read_bytes(), str(discovery), "exec")
    specs = [
        (discovery, "/usr/local/libexec/insight-capture-camera-discovery", 0o755,
         "insight-capture-camera-discovery.service"),
        (source / "hotspot/insight-capture-camera-discovery.service",
         "/etc/systemd/system/insight-capture-camera-discovery.service", 0o644,
         "insight-capture-camera-discovery.service"),
        (source / "hotspot/chrony.conf", "/etc/chrony/chrony.conf", 0o644, "chrony.service"),
    ]
    services = ["chrony.service", "insight-capture-camera-discovery.service"]
    if hotspot:
        run(["bash", "-n", str(source / "hotspot/start.sh")])
        run(["bash", "-n", str(source / "hotspot/isolate.sh")])
        run(["dnsmasq", "--test", "--conf-file=" + str(source / "hotspot/dnsmasq.conf")])
        specs += [
            (source / "hotspot/start.sh", "/usr/local/libexec/insight-capture-hotspot", 0o755,
             "insight-capture-hotspot.service"),
            (source / "hotspot/isolate.sh", "/usr/local/libexec/insight-capture-isolate", 0o755,
             "insight-capture-isolation.service"),
            (source / "hotspot/dnsmasq.conf", "/etc/insight-capture-dnsmasq.conf", 0o644,
             "insight-capture-dhcp.service"),
        ]
        for unit in ("isolation", "hotspot", "dhcp"):
            name = f"insight-capture-{unit}.service"
            specs.append((source / "hotspot" / name, "/etc/systemd/system/" + name, 0o644, name))
        services += ["insight-capture-isolation.service", "insight-capture-hotspot.service",
                     "insight-capture-dhcp.service"]
    files = [(src.read_bytes(), destination, mode, service) for src, destination, mode, service in specs]
    changed = []
    affected = set()
    for data, destination, mode, service in files:
        path = target(destination)
        if not path.is_symlink() and path.is_file() and path.read_bytes() == data and stat.S_IMODE(path.stat().st_mode) == mode:
            continue
        changed.append((data, destination, mode, service))
        affected.add(service)

    report = {"hotspot": hotspot, "deferred": defer_start, "changed": [],
              "enabled": [], "started": [], "restarted": [], "reloaded": []}
    for data, destination, mode, service in changed:
        path = target(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        if destination == "/etc/chrony/chrony.conf" and path.is_file():
            backup = path.with_name(path.name + ".before-insight-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
            shutil.copy2(path, backup)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".insight-install-", delete=False) as output:
                temporary = Path(output.name)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
                os.fchmod(output.fileno(), mode)
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        report["changed"].append(destination)

    # Retire the old reboot chain, without interrupting an already-running job.
    obsolete = target("/etc/systemd/system/insight-camera-reboot.service")
    retired = obsolete.exists() or obsolete.is_symlink()
    if retired:
        run(["systemctl", "disable", "insight-camera-reboot.service"])
        obsolete.unlink()
    if retired or any(destination.startswith("/etc/systemd/system/") for _, destination, _, _ in changed):
        run(["systemctl", "daemon-reload"])
    for service in services:
        if run(["systemctl", "is-enabled", service], check=False).stdout.strip() != "enabled":
            run(["systemctl", "enable", service])
            report["enabled"].append(service)
        if defer_start:
            continue
        # Recheck after each dependency action: restarting chrony can stop its
        # dependent discovery service even when discovery's files are unchanged.
        active = run(["systemctl", "is-active", "--quiet", service], check=False).returncode == 0
        if not active:
            action = "start"
        elif service in affected:
            # Restarting this Required oneshot would stop the working hotspot.
            action = "reload" if service == "insight-capture-isolation.service" else "restart"
        else:
            continue
        run(["systemctl", action, service])
        report[{"start": "started", "restart": "restarted", "reload": "reloaded"}[action]].append(service)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hotspot", action="store_true", help="require a configured capture hotspot")
    parser.add_argument("--defer-start", action="store_true", help="install/enable only, for first driver setup before reboot")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run with sudo on the Jetson host")
    try:
        result = install(Path(__file__).resolve().parent, hotspot=args.hotspot, defer_start=args.defer_start)
    except (OSError, RuntimeError, SyntaxError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Network service installation failed: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
