#!/bin/bash
# Run only on an idle Ubuntu 22.04 ARM64 Insight camera, from this owned bundle.
set -euo pipefail
[[ $EUID == 0 && $(uname -m) == aarch64 ]] || { echo 'Requires ARM64 camera root'; exit 1; }
if pgrep -x insight_full >/dev/null || pgrep -f '^/userdata/install/lib/insight_full_uvc/insight_full_uvc([[:space:]]|$)' >/dev/null; then
    echo 'Stop camera acquisition before installing the clock adapter' >&2
    exit 1
fi
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python3 "$here/usb_network.py" --apply
root=/opt/insight-capture-clock
mkdir -p "$root" /run/insight-capture-clock
dpkg-deb -x "$here/vendor/chrony_4.2-2ubuntu2_arm64.deb" "$root"
! ldd "$root/usr/sbin/chronyd" | grep 'not found'
# One clock owner; retire the old field verification daemon and OS clock clients.
for unit in insight-clock-verify chrony ntp systemd-timesyncd; do
    if systemctl cat "$unit.service" >/dev/null 2>&1; then
        systemctl disable --now "$unit.service"
    fi
done
install -m 0644 "$here/clock.py" "$root/clock.py"
install -m 0755 "$here/wait-sync.sh" "$root/wait-sync.sh"
install -m 0755 "$here/sync-idle.sh" "$root/sync-idle.sh"
install -m 0644 "$here/insight-capture-clock.service" /etc/systemd/system/
# Firmware init and every acquisition start use the same idle-only clock gate.
if [[ ! -f "$root/time_sync.sh.before-insight" ]]; then
    cp -a /etc/init.d/looper/time_sync.sh "$root/time_sync.sh.before-insight"
fi
install -m 0755 "$here/sync-idle.sh" /etc/init.d/looper/time_sync.sh
mkdir -p /etc/systemd/system/S99all_run.service.d
cat > /etc/systemd/system/S99all_run.service.d/insight-clock.conf <<'EOF'
[Unit]
Wants=insight-capture-clock.service
After=insight-capture-clock.service
[Service]
ExecStartPre=/opt/insight-capture-clock/sync-idle.sh
TimeoutStartSec=150
EOF
systemctl daemon-reload
systemctl enable insight-capture-clock.service
systemctl restart insight-capture-clock.service
