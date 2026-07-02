#!/usr/bin/env bash
# Discover Looper camera devices on any 169.254.0.0/16 link, reboot them in
# parallel, and wait for them to come back online.
#
# Camera IPs are not hardcoded because the network segment (the "n" in
# 169.254.n.1) can be changed at any time via `looper_cli.py network set
# --segment n`. Instead, each camera connects to this host over its own
# dedicated point-to-point link (one interface per camera, see `ip addr`),
# so devices are discovered by looking at which interfaces currently carry a
# 169.254.x.y address and deriving the camera's IP from the project's
# master/slave convention (device == <prefix>.1, host == <prefix>.2 — see
# looper_cli/README.md "network set").

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI="python3 ${SCRIPT_DIR}/../looper_cli/looper_cli.py"

DISCOVERY_TIMEOUT=40   # seconds to wait for at least one camera interface to appear
DISCOVERY_INTERVAL=2   # seconds between discovery attempts
WAIT_TIMEOUT=120        # seconds to wait for each device to come back
PING_INTERVAL=3         # seconds between ping attempts

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# Print one "http://<ip>" per camera currently reachable on a 169.254.x.x
# link, derived from local interface addresses (not a brute-force /16 scan,
# which is both slow and unreliable across a container network namespace).
discover_devices() {
    local line iface cidr ip prefix last device_ip
    ip -4 -o addr show up 2>/dev/null | while read -r line; do
        iface="$(awk '{print $2}' <<< "${line}")"
        [[ "${iface}" == "lo" || "${iface}" == docker* ]] && continue
        cidr="$(awk '{print $4}' <<< "${line}")"
        ip="${cidr%%/*}"
        [[ "${ip}" == 169.254.* ]] || continue
        prefix="${ip%.*}"
        last="${ip##*.}"
        if [[ "${last}" == "1" ]]; then
            device_ip="${prefix}.2"
        else
            device_ip="${prefix}.1"
        fi
        echo "http://${device_ip}"
    done | sort -u
}

log "Discovering cameras on 169.254.0.0/16 links..."
DEVICES=()
deadline=$(( $(date +%s) + DISCOVERY_TIMEOUT ))
while (( $(date +%s) < deadline )); do
    mapfile -t DEVICES < <(discover_devices)
    (( ${#DEVICES[@]} > 0 )) && break
    sleep "${DISCOVERY_INTERVAL}"
done

if (( ${#DEVICES[@]} == 0 )); then
    log "ERROR: No cameras found on any 169.254.x.x interface within ${DISCOVERY_TIMEOUT}s."
    log "Check that camera USB/Ethernet links are connected, and that 'ip' works in this environment."
    exit 1
fi

log "Found ${#DEVICES[@]} camera(s): ${DEVICES[*]}"

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
