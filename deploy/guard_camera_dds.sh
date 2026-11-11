#!/usr/bin/env bash
# Run once on each camera while capture is idle, then restart S99all_run.
# DDS must select the directly connected recorder address after USB IP changes.
set -euo pipefail
[[ $EUID == 0 ]] || { echo 'Run as root on the camera.' >&2; exit 1; }
file=/userdata/cyclonedds.xml
[[ -f "$file" ]]
[[ -e "$file.before-insight-direct-usb" ]] || cp -a "$file" "$file.before-insight-direct-usb"
if grep -q '<DontRoute>' "$file"; then
    sed -i 's@<DontRoute>[^<]*</DontRoute>@<DontRoute>true</DontRoute>@g' "$file"
else
    grep -q '<General>' "$file"
    sed -i 's@<General>@<General><DontRoute>true</DontRoute>@' "$file"
fi
echo 'DDS now selects only directly connected addresses, independent of the USB subnet.'
