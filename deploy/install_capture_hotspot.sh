#!/usr/bin/env bash
set -euo pipefail
[[ $EUID == 0 ]] || { echo "Run with sudo." >&2; exit 1; }
driver=${1:?Usage: install_capture_hotspot.sh DRIVER.ko UPLINK SSID}
uplink=${2:?Specify the existing Wi-Fi interface}
ssid=${3:?Specify the hotspot name}
[[ "$uplink" =~ ^[a-zA-Z0-9_-]{1,15}$ && "$uplink" != capture_ap ]]
[[ ${#ssid} -ge 1 && ${#ssid} -le 32 ]]
[[ $(modinfo -F vermagic "$driver") == "$(uname -r) "* ]]
modinfo -p "$driver" | awk '/^if2name:/ {found=1} END {exit !found}'
if [[ -z ${INSIGHT_HOTSPOT_PASSWORD:-} ]]; then
    read -rsp 'Hotspot password (8-63 characters): ' INSIGHT_HOTSPOT_PASSWORD
    echo
fi
[[ ${#INSIGHT_HOTSPOT_PASSWORD} -ge 8 && ${#INSIGHT_HOTSPOT_PASSWORD} -le 63 ]]

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
original=$(modinfo -n rtl8822ce)
backup="/var/lib/insight-capture-hotspot/$(uname -r)/rtl8822ce.stock.ko"
mkdir -p "$(dirname "$backup")"
if [[ ! -e "$backup" ]]; then install -m 0644 "$original" "$backup"; fi
install -m 0644 "$driver" "$original"
printf 'options rtl8822ce ifname=%s if2name=capture_ap\n' "$uplink" \
    > /etc/modprobe.d/insight-capture-hotspot.conf
depmod -a
printf 'UPLINK=%s\n' "$uplink" > /etc/insight-capture-hotspot.conf
printf '[main]\nfirewall-backend=iptables\n' > /etc/NetworkManager/conf.d/90-insight-hotspot.conf
if ! nmcli -g connection.uuid connection show jetson-capture-hotspot >/dev/null 2>&1; then
    nmcli connection add type wifi ifname capture_ap con-name jetson-capture-hotspot ssid "$ssid"
fi
nmcli connection modify jetson-capture-hotspot \
    connection.interface-name capture_ap connection.autoconnect no \
    802-11-wireless.ssid "$ssid" 802-11-wireless.mode ap \
    802-11-wireless-security.key-mgmt wpa-psk \
    802-11-wireless-security.proto rsn 802-11-wireless-security.pairwise ccmp \
    802-11-wireless-security.psk "$INSIGHT_HOTSPOT_PASSWORD" \
    ipv4.method manual ipv4.addresses 10.77.0.1/24 ipv4.gateway '' ipv4.dns '' \
    ipv4.never-default yes ipv6.method disabled
install -Dm 0755 "$here/hotspot/start.sh" /usr/local/libexec/insight-capture-hotspot
install -m 0755 "$here/hotspot/isolate.sh" /usr/local/libexec/insight-capture-isolate
install -m 0644 "$here/hotspot/dnsmasq.conf" /etc/insight-capture-dnsmasq.conf
install -m 0644 "$here"/hotspot/insight-capture-*.service /etc/systemd/system/
chronyd -p -f "$here/hotspot/chrony.conf" >/dev/null
[[ -e /etc/chrony/chrony.conf.before-insight-capture ]] || cp -a /etc/chrony/chrony.conf /etc/chrony/chrony.conf.before-insight-capture
install -m 0644 "$here/hotspot/chrony.conf" /etc/chrony/chrony.conf
systemctl enable chrony
systemctl daemon-reload
systemctl enable insight-capture-isolation.service insight-capture-hotspot.service insight-capture-dhcp.service
echo 'Installed. Reboot to apply the driver and NetworkManager configuration.'
echo 'Phone page: http://10.77.0.1:8765/mobile'
