#!/usr/bin/env bash
set -euo pipefail
source /etc/insight-capture-hotspot.conf
AP_CHANNEL=${AP_CHANNEL:-36}
AP_BAND=${AP_BAND:-a}
profile=jetson-capture-hotspot

disable_power_save() {
    local device=$1
    if [[ $(iw dev "$device" get power_save 2>/dev/null) == 'Power save: on' ]]; then
        iw dev "$device" set power_save off 2>/dev/null || true
    fi
}

while true; do
    if ! ip link show capture_ap >/dev/null 2>&1; then sleep 2; continue; fi
    channel=$AP_CHANNEL
    band=$AP_BAND
    station_channel=
    if iw dev "$UPLINK" link | grep -q '^Connected to '; then
        # The uplink shares this radio with the phone-facing AP.
        disable_power_save "$UPLINK"
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
    # rtl8822ce omits the channel from iw info when only its virtual AP is up.
    # Do not flap a working AP every five seconds because of that driver quirk.
    if [[ -z "$actual" && "$active" == "$profile" ]]; then
        actual=$(wpa_cli -i capture_ap status | awk -F= '$1 == "freq" {if ($2 == 2484) print 14; else if ($2 < 2500) print ($2 - 2407) / 5; else print ($2 - 5000) / 5}')
    fi
    # Losing the office connection does not require a channel change. Keep the
    # working hotspot so phones do not roam to another saved Wi-Fi network.
    # AP_CHANNEL/AP_BAND still choose the channel when starting a new hotspot.
    if [[ -z "$station_channel" && "$active" == "$profile" && "$actual" =~ ^[0-9]+$ ]]; then
        channel=$actual
        band=a
        if (( channel <= 14 )); then band=bg; fi
    fi
    if [[ "$active" != "$profile" || "$actual" != "$channel" ]]; then
        nmcli connection modify "$profile" 802-11-wireless.band "$band" 802-11-wireless.channel "$channel"
        nmcli --wait 25 connection up "$profile" ifname capture_ap || { sleep 3; continue; }
    fi
    disable_power_save capture_ap
    sleep 5
done
