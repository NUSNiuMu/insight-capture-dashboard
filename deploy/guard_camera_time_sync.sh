#!/usr/bin/env bash
# Run on an Insight camera, once, while capture is idle.
# Serialize the firmware's existing NTP client; do not install a second daemon.
set -euo pipefail
[[ $EUID == 0 ]] || { echo 'Run as root on the camera.' >&2; exit 1; }
command -v flock >/dev/null
file=/etc/init.d/looper/time_sync.sh
[[ -f "$file" ]]
if grep -q 'insight-capture-time-sync.lock' "$file"; then exit 0; fi
backup="${file}.before-insight-capture-lock"
[[ -e "$backup" ]] || cp -a "$file" "$backup"
sed -i '1a\# Serialize concurrent boot/API invocations before measuring a clock step.\nexec 9>/run/insight-capture-time-sync.lock\nflock -n 9 || exit 0\n' "$file"
bash -n "$file"
echo 'Existing camera NTP client is now serialized.'
