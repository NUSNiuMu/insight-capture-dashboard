#!/usr/bin/env bash
# Discover cameras on point-to-point links, reboot them, and await recovery.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI="python3 ${SCRIPT_DIR}/../looper_cli/looper_cli.py"

# Boot-time callers can extend discovery while USB links enumerate.
DISCOVERY_TIMEOUT="${INSIGHT_DISCOVERY_TIMEOUT:-40}"   # seconds to wait for at least one camera interface to appear
DISCOVERY_INTERVAL=2   # seconds between discovery attempts
WAIT_TIMEOUT="${INSIGHT_CAMERA_WAIT_TIMEOUT:-120}"      # seconds to wait for each device to come back
PING_INTERVAL=3         # seconds between ping attempts

log() { echo "[$(date '+%H:%M:%S')] $*"; }

configured_dds_type() {
    if [[ -n "${INSIGHT_CAMERA_DDS_TYPE:-}" ]]; then
        echo "${INSIGHT_CAMERA_DDS_TYPE}"
        return
    fi
    python3 - "${SCRIPT_DIR}/../config/cameras.json" 2>/dev/null <<'PY' || true
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("camera_dds_type", ""))
except Exception:
    pass
PY
}

current_dds_type() {
    local url="$1"
    ${CLI} --device-base-url "${url}" dds show 2>/dev/null \
        | awk -F: '/DDS Type/{gsub(/[[:space:]]/, "", $2); print $2; exit}'
}

# Use monotonic SECONDS because boot-time clock synchronization can jump.

# Wait for the configured camera count because USB links enumerate gradually.
expected_camera_count() {
    python3 - "${SCRIPT_DIR}/../config/cameras.json" 2>/dev/null <<'PY' || echo 0
import json, sys
try:
    with open(sys.argv[1]) as fh:
        print(len(json.load(fh).get("cameras", [])))
except Exception:
    print(0)
PY
}

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

EXPECTED_COUNT="$(expected_camera_count)"
if (( EXPECTED_COUNT > 0 )); then
    log "Discovering cameras on 169.254.0.0/16 links (expecting ${EXPECTED_COUNT} per config/cameras.json)..."
else
    log "Discovering cameras on 169.254.0.0/16 links (no expected count available)..."
fi
DEVICES=()
discovery_deadline=$(( SECONDS + DISCOVERY_TIMEOUT ))
while (( SECONDS < discovery_deadline )); do
    mapfile -t DEVICES < <(discover_devices)
    if (( EXPECTED_COUNT > 0 )); then
        (( ${#DEVICES[@]} >= EXPECTED_COUNT )) && break
    else
        (( ${#DEVICES[@]} > 0 )) && break
    fi
    sleep "${DISCOVERY_INTERVAL}"
done

if (( ${#DEVICES[@]} == 0 )); then
    log "ERROR: No cameras found on any 169.254.x.x interface within ${DISCOVERY_TIMEOUT}s."
    log "Check that camera USB/Ethernet links are connected, and that 'ip' works in this environment."
    exit 1
fi

if (( EXPECTED_COUNT > 0 && ${#DEVICES[@]} < EXPECTED_COUNT )); then
    log "WARNING: Only ${#DEVICES[@]}/${EXPECTED_COUNT} camera link(s) appeared within ${DISCOVERY_TIMEOUT}s."
    log "WARNING: Cameras on links that come up later will keep a stale DDS participant and stream nothing."
fi

log "Found ${#DEVICES[@]} camera(s): ${DEVICES[*]}"

wait_for_device() {
    local url="$1"
    local host="${url#http://}"
    local deadline=$(( SECONDS + WAIT_TIMEOUT ))
    log "Waiting for ${host} to come back online..."
    while (( SECONDS < deadline )); do
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
TARGET_DDS_TYPE="$(configured_dds_type)"
if [[ -n "${TARGET_DDS_TYPE}" && "${TARGET_DDS_TYPE}" != "cyclonedds" \
        && "${TARGET_DDS_TYPE}" != "fastrtps" ]]; then
    log "ERROR: unsupported camera_dds_type '${TARGET_DDS_TYPE}'"
    exit 1
fi

reboot_or_configure_device() {
    local url="$1" current
    current="$(current_dds_type "${url}" || true)"
    if [[ -n "${TARGET_DDS_TYPE}" && "${current}" != "${TARGET_DDS_TYPE}" ]]; then
        log "Changing ${url} DDS mode ${current:-unknown} -> ${TARGET_DDS_TYPE} (device reboots)..."
        # Some firmware closes HTTP immediately after accepting the setting,
        # so a non-zero CLI exit can still mean the reboot was initiated.
        ${CLI} --device-base-url "${url}" dds set "${TARGET_DDS_TYPE}" -y || true
    else
        log "Rebooting ${url} (DDS=${current:-unchanged})..."
        ${CLI} --device-base-url "${url}" system reboot -y || true
    fi
}

log "Preparing all cameras (target DDS=${TARGET_DDS_TYPE:-unchanged})..."
pids=()
for url in "${DEVICES[@]}"; do
    reboot_or_configure_device "${url}" &
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
