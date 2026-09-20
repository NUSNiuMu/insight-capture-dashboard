"""Maintain Linux DDS discovery membership across camera USB netdev recreation."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
from pathlib import Path
import socket
import struct
import threading
import time


_DISCOVERY_GROUP = "239.255.0.1"


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


class DiscoveryMemberships:
    """Keep default Fast DDS multicast reachable by its existing wildcard sockets."""

    def __init__(self, logger) -> None:
        self._logger = logger
        self._sockets: dict[CameraLink, socket.socket] = {}
        self._last_warning_at: dict[object, float] = {}
        self._lock = threading.Lock()
        self._closed = False

    def _warn(self, key: object, message: str) -> None:
        now = time.monotonic()
        if now - self._last_warning_at.get(key, float("-inf")) >= 60.0:
            self._last_warning_at[key] = now
            self._logger.warning(message)

    def refresh(self) -> None:
        """Rejoin recreated interfaces; retry failures on the next watchdog tick."""
        with self._lock:
            if self._closed:
                return
            try:
                links = camera_links()
            except OSError as exc:
                self._warn("scan", f"DDS discovery interface scan failed: {exc}")
                return
            self._last_warning_at.pop("scan", None)
            for link in self._sockets.keys() - links:
                self._sockets.pop(link).close()
                self._logger.info(
                    f"DDS discovery released {link.name} ifindex={link.index} ({link.address})"
                )
            for key in list(self._last_warning_at):
                if key not in links:
                    self._last_warning_at.pop(key, None)
            for link in sorted(links - self._sockets.keys(), key=lambda item: item.name):
                sock = None
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    # No bind/reader: Linux IP_MULTICAST_ALL lets the existing
                    # Fast DDS wildcard sockets receive this globally joined group.
                    sock.setsockopt(
                        socket.IPPROTO_IP,
                        socket.IP_ADD_MEMBERSHIP,
                        struct.pack(
                            "=4s4si",
                            socket.inet_aton(_DISCOVERY_GROUP),
                            socket.inet_aton(link.address),
                            link.index,
                        ),
                    )
                except OSError as exc:
                    if sock is not None:
                        sock.close()
                    self._warn(link, f"DDS discovery join failed on {link.name} ifindex={link.index}: {exc}")
                    continue
                self._sockets[link] = sock
                self._last_warning_at.pop(link, None)
                self._logger.info(
                    f"DDS discovery joined {_DISCOVERY_GROUP} on {link.name} "
                    f"ifindex={link.index} ({link.address}); existing participants retained"
                )

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for sock in self._sockets.values():
                sock.close()
            self._sockets.clear()
            self._last_warning_at.clear()
