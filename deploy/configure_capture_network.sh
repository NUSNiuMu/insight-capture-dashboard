#!/usr/bin/env bash
# Upgrade the existing AP configuration without replacing the Wi-Fi driver.
set -euo pipefail
[[ $EUID == 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
for tool in nmcli iw wpa_cli dnsmasq iptables chronyc; do command -v "$tool" >/dev/null; done
[[ -f /etc/insight-capture-hotspot.conf ]]
nmcli connection show jetson-capture-hotspot >/dev/null
# Do not accept a network change while the recorder state is unknown or busy.
curl -fsS --max-time 10 http://127.0.0.1:8765/api/mobile/status | python3 -c '
import json,sys
s=json.load(sys.stdin)
assert not s["recording"] and s["state"] not in {"starting","saving"}, "Capture is busy"
'
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
backup=/var/lib/insight-capture-network/$(date +%Y%m%d-%H%M%S)
mkdir -p "$backup"
chmod 700 "$backup"
cp -a /etc/chrony/conf.d /etc/NetworkManager/system-connections /etc/insight-capture-hotspot.conf "$backup/"
# The old AP owner must not compete for the same virtual interface/profile.
for unit in jetson-hotspot.service jetson-hotspot-dhcp.service insight-capture-hotspot.service; do
    if systemctl cat "$unit" >/dev/null 2>&1; then systemctl disable --now "$unit"; fi
done
nmcli connection down jetson-capture-hotspot 2>/dev/null || true
nmcli connection modify jetson-capture-hotspot connection.autoconnect no \
    ipv4.method manual ipv4.addresses 10.77.0.1/24 ipv4.gateway '' ipv4.dns '' \
    ipv4.never-default yes ipv6.method disabled
install -m 0755 "$here/hotspot/start.sh" /usr/local/libexec/insight-capture-hotspot
install -m 0755 "$here/hotspot/isolate.sh" /usr/local/libexec/insight-capture-isolate
# The supervisor handles STA channel changes and offline startup itself.
rm -f /etc/NetworkManager/dispatcher.d/90-insight-hotspot
install -m 0644 "$here/hotspot/dnsmasq.conf" /etc/insight-capture-dnsmasq.conf
install -m 0644 "$here"/hotspot/insight-capture-*.service /etc/systemd/system/
cat > /etc/chrony/conf.d/insight-cameras.conf <<'CONFIG'
# Isolated USB camera links, including the Insight9 middle camera.
allow 169.254.10.0/24
allow 169.254.20.0/24
allow 169.254.30.0/24
allow 169.254.40.0/24
CONFIG
# Retain the existing local-reference filename so no duplicate is installed.
printf 'local stratum 10\n' > /etc/chrony/conf.d/jetson-hotspot.conf
chronyc allow 169.254.40.0/24
systemctl enable chrony
systemctl daemon-reload
systemctl enable --now insight-capture-isolation.service insight-capture-hotspot.service insight-capture-dhcp.service
printf 'Capture network configured. Backup: %s\nPhone: http://10.77.0.1:8765/mobile\n' "$backup"
