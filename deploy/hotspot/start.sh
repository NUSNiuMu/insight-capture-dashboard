#!/usr/bin/env bash
set -euo pipefail
source /etc/insight-capture-hotspot.conf

# AP and station share one radio and must use the same channel.
iw dev "$UPLINK" link | grep -q '^Connected to ' || {
    echo "Connect $UPLINK to Wi-Fi first." >&2; exit 1;
}
channel=$(iw dev "$UPLINK" info | awk '$1 == "channel" {print $2}')
[[ -n "$channel" ]] || { echo "Connect $UPLINK to Wi-Fi first." >&2; exit 1; }
band=a
if (( channel <= 14 )); then band=bg; fi
nmcli connection modify jetson-capture-hotspot \
    802-11-wireless.band "$band" 802-11-wireless.channel "$channel"
nmcli --wait 30 connection up jetson-capture-hotspot ifname capture_ap
