#!/usr/bin/env python3
"""Keep the camera USB startup command consistent with its declared network."""
import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import subprocess
import tempfile

BOOT_LINE = re.compile(r'(?m)^(\s*(?:/sbin/)?ifconfig\s+usb0\s+)([0-9.]+)(?:\s+netmask\s+[0-9.]+)?[ \t]*$')
OLD_GENERATOR = 'echo \'ifconfig usb0 \'$NEW_IP\'\' >> "$IP_SCRIPT_FILE"'
NEW_GENERATOR = re.compile(r"(?m)^printf 'ifconfig usb0 %s netmask %s\\n' \"\$NEW_IP\" \"[0-9.]+\" >> \"\$IP_SCRIPT_FILE\"$")

def repaired_scripts(network, boot, generator):
    addresses = re.findall(r'(?m)^[ \t]*Address\s*=\s*([^\s#]+)', network)
    configured = [ipaddress.IPv4Interface(value) for value in addresses if ':' not in value]
    if len(configured) != 1:
        raise ValueError('Expected one configured USB IPv4 address')
    address = configured[0]
    matches = list(BOOT_LINE.finditer(boot))
    if len(matches) != 1 or ipaddress.IPv4Address(matches[0][2]) != address.ip:
        raise ValueError('USB startup address does not match its network configuration')
    boot = BOOT_LINE.sub(lambda m: f'{m[1]}{m[2]} netmask {address.netmask}', boot)
    prefixes = set(re.findall(r'Address=\$NEW_IP\\?/(\d+)', generator))
    if len(prefixes) != 1:
        raise ValueError('Cannot identify the IP generator network prefix')
    mask = ipaddress.IPv4Network('0.0.0.0/' + prefixes.pop()).netmask
    replacement = f'''printf 'ifconfig usb0 %s netmask %s\\n' "$NEW_IP" "{mask}" >> "$IP_SCRIPT_FILE"'''
    if generator.count(OLD_GENERATOR) == 1:
        generator = generator.replace(OLD_GENERATOR, replacement)
    elif len(NEW_GENERATOR.findall(generator)) == 1:
        generator = NEW_GENERATOR.sub(lambda _: replacement, generator)
    else:
        raise ValueError('Unrecognized USB startup generator')
    return boot, generator, address

def install(root=Path('/'), apply=False):
    directory = root / 'etc/init.d/looper'
    boot_path, generator_path = directory/'ip_config.sh', directory/'modify_ip.sh'
    original = [path.read_text() for path in [boot_path, generator_path]]
    boot, generator, address = repaired_scripts((root/'etc/systemd/network/usb0.network').read_text(), *original)
    if apply:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
            request = struct.pack('256s', b'usb0')
            current = socket.inet_ntoa(fcntl.ioctl(control.fileno(), 0x8915, request)[20:24])
            if current != str(address.ip):
                raise ValueError('Live USB address does not match its network configuration')
    prepared = []
    try:
        for path, before, after in zip([boot_path, generator_path], original, [boot, generator]):
            if before == after:
                continue
            with tempfile.NamedTemporaryFile(mode='w', prefix='.insight-usb-', dir=directory, delete=False) as stream:
                temporary = Path(stream.name)
                prepared.append((path, temporary))
                stream.write(after)
            temporary.chmod(stat.S_IMODE(path.stat().st_mode))
            subprocess.run(['bash', '-n', str(temporary)], check=True)
        for path, temporary in prepared:
            temporary.replace(path)
    finally:
        for _, temporary in prepared:
            temporary.unlink(missing_ok=True)
    if apply:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
            request = struct.pack('16sH2x4s8x', b'usb0', socket.AF_INET, socket.inet_aton(str(address.netmask)))
            fcntl.ioctl(control.fileno(), 0x891c, request)
            actual = socket.inet_ntoa(fcntl.ioctl(control.fileno(), 0x891b, struct.pack('256s', b'usb0'))[20:24])
            if actual != str(address.netmask):
                raise RuntimeError('USB mask did not take effect')
    if prepared:
        subprocess.run(['sync'], check=True)
    return {'address': str(address.ip), 'netmask': str(address.netmask), 'changed': [str(path) for path, _ in prepared], 'applied': apply}

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    arguments = parser.parse_args()
    if arguments.apply:
        if os.geteuid() != 0:
            raise SystemExit('Apply requires camera root')
        acquisition_checks = [
            ['pgrep', '-x', 'insight_full'],
            ['pgrep', '-f', '^/userdata/install/lib/insight_full_uvc/insight_full_uvc([[:space:]]|$)'],
        ]
        if any(subprocess.run(command, stdout=subprocess.DEVNULL).returncode == 0
               for command in acquisition_checks):
            raise SystemExit('Stop camera acquisition before applying USB network settings')
    print(json.dumps(install(apply=arguments.apply)))
