#!/usr/bin/env bash
set -euo pipefail
source /etc/insight-capture-hotspot.conf
AP_CHANNEL=${AP_CHANNEL:-36}
AP_BAND=${AP_BAND:-a}
profile=jetson-capture-hotspot
while true; do
    if ! ip link show capture_ap >/dev/null 2>&1; then sleep 2; continue; fi
    channel=$AP_CHANNEL
    band=$AP_BAND
    if iw dev "$UPLINK" link | grep -q '^Connected to '; then
        # A single radio can run STA + AP only on its current station channel.
        station_channel=$(iw dev "$UPLINK" info | awk '$1 == "channel" {print $2}')
        if [[ -n "$station_channel" ]]; then
            channel=$station_channel
            band=a
            if (( channel <= 14 )); then band=bg; fi
        fi
    fi
    actual=$(iw dev capture_ap info | awk '$1 == "channel" {print $2}')
    active=$(nmcli -g GENERAL.CONNECTION device show capture_ap)
    if [[ "$active" != "$profile" || "$actual" != "$channel" ]]; then
        nmcli connection modify "$profile" 802-11-wireless.band "$band" 802-11-wireless.channel "$channel"
        nmcli --wait 25 connection up "$profile" ifname capture_ap || { sleep 3; continue; }
    fi
    sleep 5
done
