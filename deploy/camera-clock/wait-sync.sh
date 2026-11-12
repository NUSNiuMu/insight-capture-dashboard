#!/bin/bash
# Acquisition gate; no clock-setting command, even when called by the API.
set -eu
for attempt in $(seq 1 120); do
    if /opt/insight-capture-clock/usr/bin/chronyc -h /run/insight-capture-clock/chronyd.sock waitsync 1 0.005 0 0.1; then
        exit 0
    fi
    sleep 1
done
exit 1
