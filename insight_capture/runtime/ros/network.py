"""Keep DDS discovery on the configured capture interfaces."""

import atexit
import ctypes
import fcntl
import ipaddress
import os
from pathlib import Path
import socket
import struct
import tempfile
import xml.etree.ElementTree as ET
from insight_capture.runtime.discovery import camera_links
from insight_capture.core.paths import PROJECT_ROOT

_dynamic_profile = None
_transport_library = None


def local_ipv4_addresses():
    """Return assigned addresses on interfaces that are currently up."""
    addresses = set()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        for _, name in socket.if_nameindex():
            request = struct.pack("256s", name.encode()[:15])
            try:
                flags = struct.unpack_from("H", fcntl.ioctl(control, 0x8913, request), 16)[0]
                if flags & 1:
                    address = fcntl.ioctl(control, 0x8915, request)[20:24]
                    addresses.add(socket.inet_ntoa(address))
            except OSError:
                continue
    return addresses


def configure_ros_network(config):
    """Apply an optional Fast DDS transport profile before the first ROS init."""
    transport = config.get("ros_transport", {})
    included = transport.get("include_host_addresses", [])
    excluded = transport.get("exclude_host_addresses", [])
    dynamic = transport.get("camera_usb_only", False)
    if not dynamic and not included and not excluded:
        return None
    excluded = {str(ipaddress.IPv4Address(address)) for address in excluded}
    if os.environ.get("RMW_IMPLEMENTATION", "rmw_fastrtps_cpp") not in {
        "rmw_fastrtps_cpp", "rmw_fastrtps_dynamic_cpp"
    }:
        return None
    if any(os.environ.get(key) for key in (
        "FASTRTPS_DEFAULT_PROFILES_FILE", "FASTDDS_DEFAULT_PROFILES_FILE"
    )):
        return None

    addresses = ({link.address for link in camera_links()} if dynamic else local_ipv4_addresses()) - excluded
    if included and not dynamic:
        addresses &= {str(ipaddress.IPv4Address(address)) for address in included}
    addresses.add("127.0.0.1")
    path = write_profile(addresses)
    atexit.register(path.unlink, missing_ok=True)
    os.environ["FASTRTPS_DEFAULT_PROFILES_FILE"] = str(path)
    if dynamic:
        global _dynamic_profile
        _dynamic_profile = path
        refresh_ros_network()
    return path


def write_profile(addresses, path=None):
    root = ET.Element(
        "profiles", xmlns="http://www.eprosima.com/XMLSchemas/fastRTPS_Profiles"
    )
    transports = ET.SubElement(root, "transport_descriptors")
    udp = ET.SubElement(transports, "transport_descriptor")
    ET.SubElement(udp, "transport_id").text = "insight_udp"
    ET.SubElement(udp, "type").text = "UDPv4"
    whitelist = ET.SubElement(udp, "interfaceWhiteList")
    for address in sorted(addresses):
        ET.SubElement(whitelist, "address").text = address
    shm = ET.SubElement(transports, "transport_descriptor")
    ET.SubElement(shm, "transport_id").text = "insight_shm"
    ET.SubElement(shm, "type").text = "SHM"
    participant = ET.SubElement(
        root, "participant", profile_name="insight_network", is_default_profile="true"
    )
    rtps = ET.SubElement(participant, "rtps")
    user_transports = ET.SubElement(rtps, "userTransports")
    for transport in ("insight_udp", "insight_shm"):
        ET.SubElement(user_transports, "transport_id").text = transport
    ET.SubElement(rtps, "useBuiltinTransports").text = "false"
    if path is None:
        with tempfile.NamedTemporaryFile(prefix="insight-fastdds-", suffix=".xml", delete=False) as profile:
            path = Path(profile.name)
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))
    temporary.replace(path)
    return path


def refresh_ros_network():
    """Update both future child processes and Fast DDS's in-process QoS cache."""
    if _dynamic_profile is None:
        return
    addresses = sorted({'127.0.0.1'} | {link.address for link in camera_links()})
    global _transport_library
    if _transport_library is None:
        _transport_library = ctypes.CDLL(str(PROJECT_ROOT / '.native-build/libinsight_dds_network.so'))
        _transport_library.insight_set_capture_addresses.argtypes = [ctypes.c_char_p]
        _transport_library.insight_set_capture_addresses.restype = ctypes.c_int
    if _transport_library.insight_set_capture_addresses(','.join(addresses).encode()) != 0:
        raise RuntimeError('Cannot refresh the USB-only DDS transport')
    write_profile(addresses, _dynamic_profile)
