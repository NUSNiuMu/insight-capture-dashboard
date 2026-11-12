#!/usr/bin/env python3
"""Own a single offline camera clock; follow the USB DHCP peer without steps."""
import ipaddress
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

ROOT = Path('/opt/insight-capture-clock')
RUN = Path('/run/insight-capture-clock')
SOCKET = str(RUN / 'chronyd.sock')
SYNC_FLAG = Path('/etc/init.d/looper/setting/is_time_sync')


def publish_sync_state(synced):
    """Readers must see a complete flag, including when readiness is unchanged.

    The camera publisher treats an empty file as sync loss and switches its
    headers to CLOCK_MONOTONIC. Truncating this file can therefore change the
    timestamp domain even while chronyd remains synchronized.
    """
    value = '1\n' if synced else '0\n'
    try:
        if SYNC_FLAG.read_bytes() == value.encode():
            return
    except FileNotFoundError:
        pass
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=SYNC_FLAG.parent,
                                         prefix='.is_time_sync-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(value)
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
        os.replace(temporary, SYNC_FLAG)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

def peer():
    for line in Path('/etc/dnsmasq.conf').read_text().splitlines():
        if line.startswith('dhcp-range='):
            return str(ipaddress.IPv4Address(line.split('=', 1)[1].split(',')[0]))
    raise ValueError('No USB DHCP peer configured')

def command(*args):
    return subprocess.run([str(ROOT / 'usr/bin/chronyc'), '-h', SOCKET, *args],
                          capture_output=True, text=True, timeout=4, check=True)

def main():
    RUN.mkdir(parents=True, exist_ok=True)
    RUN.chmod(0o700)
    # Stepping is confined to the acquisition pre-start gate. A low slew limit
    # keeps the hardware timestamp clock close to CLOCK_MONOTONIC while running.
    config = [f'driftfile {ROOT}/drift', f'pidfile {RUN}/chronyd.pid',
              f'bindcmdaddress {SOCKET}', 'cmdport 0', 'deny all',
              'rtcsync', 'maxupdateskew 1000', 'maxslewrate 100']
    (RUN / 'chrony.conf').write_text('\n'.join(config) + '\n')
    daemon = subprocess.Popen([str(ROOT / 'usr/sbin/chronyd'), '-d', '-u', 'root',
                               '-f', str(RUN / 'chrony.conf')])
    def stop(*_):
        daemon.terminate()
        raise SystemExit()
    signal.signal(signal.SIGTERM, stop)
    current = None
    try:
        while daemon.poll() is None:
            try:
                target = peer()
                if target != current:
                    if current:
                        command('deny', current)
                        try:
                            command('delete', current)
                        except subprocess.CalledProcessError:
                            pass  # A previous attempt may already have removed it.
                        current = None
                    # Reconcile a partially accepted update after command failure.
                    try:
                        command('delete', target)
                    except subprocess.CalledProcessError:
                        pass
                    command('add', 'server', target, 'iburst', 'minpoll', '0', 'maxpoll', '3')
                    command('allow', target)
                    current = target
                    (RUN / 'peer').write_text(target + '\n')
                    print(f'USB clock source: {target}', flush=True)
                # Export live readiness for the camera's existing API.
                synced = command('waitsync', '1', '0.005', '0', '0.1').returncode == 0
                publish_sync_state(synced)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                publish_sync_state(False)
                print(str(exc), flush=True)
            time.sleep(2)
        raise SystemExit(daemon.returncode or 1)
    finally:
        if daemon.poll() is None:
            daemon.terminate()
            daemon.wait(timeout=5)

if __name__ == '__main__':
    main()
