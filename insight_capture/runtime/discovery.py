"""Identify camera network generations independently of device IP assignments."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
from pathlib import Path
import socket
import struct

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
                if address.startswith("169.254.") and socket.if_nametoindex(name) == index:
                    links.add(CameraLink(name, index, address))
            except OSError:
                # udev may rename/remove a link or assign its address mid-scan.
                continue
    return links
