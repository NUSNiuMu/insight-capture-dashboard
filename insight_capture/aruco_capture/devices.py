"""Discover stable Linux device paths without starting video streams."""

import fcntl
import os
import re
import struct
from pathlib import Path


def device_root():
    return Path(os.environ.get("ARUCO_DEVICE_ROOT", "/dev"))


def local_device(path):
    if isinstance(path, int):
        path = f"/dev/video{path}"
    return str(device_root() / str(path).removeprefix("/dev/"))


def valid_device(path, kind):
    pattern = (
        r"/dev/(video\d+|v4l/(by-id|by-path)/[^/]+)"
        if kind == "video"
        else r"/dev/(tty(USB|ACM)\d+|serial/(by-id|by-path)/[^/]+)"
    )
    return isinstance(path, str) and re.fullmatch(pattern, path) is not None


def scan_devices():
    root = device_root()
    found = {"video": [], "serial": []}
    for kind, patterns, directory in (
        ("video", ("video[0-9]*",), "v4l"),
        ("serial", ("ttyUSB[0-9]*", "ttyACM[0-9]*"), "serial"),
    ):
        aliases = {}
        # by-id survives a different USB socket; by-path identifies a socket.
        for flavor in ("by-path", "by-id"):
            for link in sorted((root / directory / flavor).glob("*")):
                if link.exists():
                    aliases[str(link.resolve())] = "/dev/" + str(link.relative_to(root))
        for node in sorted(p for pattern in patterns for p in root.glob(pattern)):
            if not node.is_char_device():
                continue
            label, error = node.name, None
            if kind == "video":
                try:
                    with node.open("rb", buffering=0) as handle:
                        caps = bytearray(104)
                        fcntl.ioctl(handle.fileno(), 0x80685600, caps, True)
                    capabilities, device_caps = struct.unpack_from("II", caps, 84)
                    bits = device_caps if capabilities & 0x80000000 else capabilities
                    if not bits & (0x1 | 0x1000):
                        continue
                    label = bytes(caps[16:48]).split(b"\0")[0].decode(errors="replace")
                except OSError as exc:
                    error = str(exc)
            else:
                entry = Path("/sys/class/tty") / node.name / "device"
                try:
                    label = next(
                        (
                            p.joinpath("product").read_text().strip()
                            for p in entry.resolve().parents
                            if p.joinpath("product").is_file()
                        ),
                        node.name,
                    )
                except OSError:
                    pass
            device = aliases.get(str(node.resolve()), "/dev/" + node.name)
            found[kind].append(
                {
                    "device": device,
                    "node": "/dev/" + node.name,
                    "label": label,
                    "stable": "/by-" in device,
                    "error": error,
                }
            )
    return found
