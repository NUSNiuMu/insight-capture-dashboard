#!/usr/bin/env bash
set -euo pipefail
source /etc/insight-capture-hotspot.conf
if [[ "$1" == "$UPLINK" && "$2" == up ]]; then
    systemctl --no-block restart insight-capture-hotspot.service
fi
