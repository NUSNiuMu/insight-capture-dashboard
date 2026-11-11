#!/usr/bin/env bash
set -euo pipefail
[[ $EUID == 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
install -Dm 0755 "$here/../insight_capture/runtime/discovery.py" /usr/local/libexec/insight-capture-camera-discovery
install -m 0644 "$here/hotspot/insight-capture-camera-discovery.service" /etc/systemd/system/
chronyd -p -f "$here/hotspot/chrony.conf" >/dev/null
if ! cmp -s "$here/hotspot/chrony.conf" /etc/chrony/chrony.conf; then
    cp -a /etc/chrony/chrony.conf "/etc/chrony/chrony.conf.before-discovery-$(date +%Y%m%d-%H%M%S)"
    install -m 0644 "$here/hotspot/chrony.conf" /etc/chrony/chrony.conf
    systemctl restart chrony
fi
systemctl daemon-reload
systemctl enable --now insight-capture-camera-discovery.service
systemctl restart insight-capture-camera-discovery.service
