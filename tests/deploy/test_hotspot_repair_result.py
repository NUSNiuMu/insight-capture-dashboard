"""File delivery and device-side readiness checks for the offline repair package."""
import os
from pathlib import Path
import subprocess

import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / "deploy/hotspot-init"


def test_failure_updates_the_same_shareable_file_and_redacts_secrets(tmp_path):
    destination = tmp_path / "Downloads with spaces" / "result.txt"
    destination.parent.mkdir()
    destination.write_text("等待重启验证")
    (tmp_path / "destination").write_text(str(destination) + "\n")
    (tmp_path / "owner").write_text(f"{os.getuid()} {os.getgid()}\n")
    (tmp_path / "install.log").write_text("APT error\nWi-Fi 密码：test-private-value\n")
    script = '''
        set -euo pipefail
        source "$1/report.sh"
        collect() { echo 'driver diagnostic'; echo 'psk=test-private-value'; return 1; }
        failure_report "$2" '重启后验证' 100
    '''
    result = subprocess.run(["bash", "-c", script, "test", str(SCRIPTS), str(tmp_path)],
                            check=True, text=True, capture_output=True)
    report = destination.read_text()
    assert str(destination) in result.stdout
    assert report == (tmp_path / "result.txt").read_text()
    assert all(value in report for value in ("重启后验证", "退出码：100", "APT error", "driver diagnostic"))
    assert "test-private-value" not in report
    assert list(destination.parent.iterdir()) == [destination]


@pytest.mark.parametrize("failed,reason", [
    ("", ""), ("iw", "AP 模式"), ("nmcli", "NetworkManager"), ("ip", "10.77.0.1/24"),
    ("dhcp", "dhcp"), ("python3", "HTTP 200"),
])
def test_current_status_requires_the_hotspot_and_page(tmp_path, failed, reason):
    shim = tmp_path / "shim"
    shim.write_text("""#!/bin/sh
name=${0##*/}
[ "$name" != "$FAILED" ] || exit 1
case "$name" in
iw) echo '    type AP' ;;
nmcli) echo jetson-capture-hotspot ;;
ip) echo '3: capture_ap inet 10.77.0.1/24 scope global capture_ap' ;;
systemctl) [ "$3" != "insight-capture-$FAILED.service" ] ;;
esac
""")
    shim.chmod(0o755)
    for tool in ("iw", "nmcli", "ip", "systemctl", "python3"):
        (tmp_path / tool).symlink_to(shim)
    result = subprocess.run(["bash", str(SCRIPTS / "verify.sh"), "--current"],
                            env={**os.environ, "FAILED": failed, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
                            text=True, capture_output=True)
    assert result.returncode == bool(failed)
    assert reason in result.stdout
