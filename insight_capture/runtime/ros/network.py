"""Keep local-domain DDS locators away from optional camera USB links."""

import atexit
import fcntl
import ipaddress
import os
from pathlib import Path
import socket
import struct
import tempfile
import xml.etree.ElementTree as ET


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
    excluded = config.get("ros_transport", {}).get("exclude_host_addresses", [])
    if not excluded:
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

    addresses = (local_ipv4_addresses() - excluded) | {"127.0.0.1"}
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
    with tempfile.NamedTemporaryFile(prefix="insight-fastdds-", suffix=".xml", delete=False) as profile:
        profile.write(ET.tostring(root, encoding="utf-8", xml_declaration=True))
    path = Path(profile.name)
    atexit.register(path.unlink, missing_ok=True)
    os.environ["FASTRTPS_DEFAULT_PROFILES_FILE"] = str(path)
    return path
