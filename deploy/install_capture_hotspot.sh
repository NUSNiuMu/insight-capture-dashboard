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
[[ ${#INSIGHT_HOTSPOT_PASSWORD} -ge 8 && ${#INSIGHT_HOTSPOT_PASSWORD} -le 63 ||
   $INSIGHT_HOTSPOT_PASSWORD =~ ^[[:xdigit:]]{64}$ ]]

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
original=$(modinfo -n rtl8822ce)
backup="/var/lib/insight-capture-hotspot/$(uname -r)/rtl8822ce.stock.ko"
mkdir -p "$(dirname "$backup")" /etc/modprobe.d /etc/NetworkManager/conf.d
if ! cmp -s "$driver" "$original"; then
    staged=$(mktemp "$(dirname "$original")/.insight-rtl8822ce.XXXXXX")
    trap 'rm -f -- "$staged"' EXIT
    if [[ ! -e "$backup" ]]; then
        temp_backup=$(mktemp "$(dirname "$backup")/.stock.XXXXXX")
        install -m 0644 "$original" "$temp_backup"
        mv -fT "$temp_backup" "$backup"
    fi
    install -m 0644 "$driver" "$staged"
    mv -fT "$staged" "$original"
fi
printf 'options rtl8822ce ifname=%s if2name=capture_ap\n' "$uplink" \
    > /etc/modprobe.d/insight-capture-hotspot.conf
depmod -a
config=/etc/insight-capture-hotspot.conf
if [[ -f $config ]] && grep -q '^UPLINK=' "$config"; then
    sed -i "s/^UPLINK=.*/UPLINK=$uplink/" "$config"
else
    printf '\nUPLINK=%s\n' "$uplink" >> "$config"
fi
printf '[main]\nfirewall-backend=iptables\n' > /etc/NetworkManager/conf.d/90-insight-hotspot.conf
if ! nmcli -g connection.uuid connection show jetson-capture-hotspot >/dev/null 2>&1; then
    nmcli connection add type wifi ifname capture_ap con-name jetson-capture-hotspot ssid "$ssid"
fi
nmcli connection modify jetson-capture-hotspot \
    connection.interface-name capture_ap connection.autoconnect no \
    802-11-wireless.ssid "$ssid" 802-11-wireless.mode ap 802-11-wireless.powersave 2 \
    802-11-wireless.hidden no 802-11-wireless-security.psk-flags 0 \
    802-11-wireless-security.key-mgmt wpa-psk \
    802-11-wireless-security.proto rsn 802-11-wireless-security.pairwise ccmp \
    802-11-wireless-security.psk "$INSIGHT_HOTSPOT_PASSWORD" \
    ipv4.method manual ipv4.addresses 10.77.0.1/24 ipv4.gateway '' ipv4.dns '' \
    ipv4.never-default yes ipv6.method disabled
python3 "$here/install_network_services.py" --hotspot --defer-start
echo '已写入热点驱动和网络服务配置，新写入的驱动将在重启后加载。'
echo 'Phone page: http://10.77.0.1'
