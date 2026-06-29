#!/usr/bin/env bash
# Reboot all three Looper camera devices in parallel and wait for them to come back online.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI="python3 ${SCRIPT_DIR}/../looper_cli/looper_cli.py"

DEVICES=(
    "http://169.254.30.1"
    "http://169.254.40.1"
    "http://169.254.50.1"
)

WAIT_TIMEOUT=120   # seconds to wait for each device to come back
PING_INTERVAL=3    # seconds between ping attempts

log() { echo "[$(date '+%H:%M:%S')] $*"; }

wait_for_device() {
    local url="$1"
    local host="${url#http://}"
    local deadline=$(( $(date +%s) + WAIT_TIMEOUT ))
    log "Waiting for ${host} to come back online..."
    while (( $(date +%s) < deadline )); do
        if ping -c 1 -W 1 "${host}" &>/dev/null; then
            log "${host} is back online"
            return 0
        fi
        sleep "${PING_INTERVAL}"
    done
    log "WARNING: ${host} did not respond within ${WAIT_TIMEOUT}s"
    return 1
}

# Wait for all network interfaces carrying 169.254.x.x to be up before starting
log "Checking network readiness..."
for url in "${DEVICES[@]}"; do
    host="${url#http://}"
    for i in $(seq 1 20); do
        if ip route get "${host}" &>/dev/null; then
            break
        fi
        sleep 2
    done
done

# Send reboot command to all devices in parallel
log "Sending reboot command to all cameras..."
pids=()
for url in "${DEVICES[@]}"; do
    log "Rebooting ${url}..."
    ${CLI} --device-base-url "${url}" system reboot -y &
    pids+=($!)
done

# Wait for all reboot commands to complete
for pid in "${pids[@]}"; do
    wait "${pid}" || true
done

log "Reboot commands sent. Waiting for devices to come back..."
sleep 10   # give devices time to start shutting down before pinging

# Wait for all devices to come back online
all_ok=true
for url in "${DEVICES[@]}"; do
    wait_for_device "${url}" || all_ok=false
done

if $all_ok; then
    log "All cameras are back online."
else
    log "WARNING: One or more cameras did not come back within timeout."
    exit 1
fi
