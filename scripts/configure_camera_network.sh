#!/usr/bin/env bash
# Spread camera USB-Ethernet receive processing across the non-IRQ CPU cores.

set -euo pipefail

log() { echo "[camera_network] $*"; }

if [[ "${EUID}" -ne 0 ]]; then
    echo "[camera_network] ERROR: run as root (normally via systemd)." >&2
    exit 1
fi

cpu_count="$(nproc)"
if (( cpu_count < 2 || cpu_count > 31 )); then
    log "WARNING: unsupported online CPU count ${cpu_count}; leaving RPS unchanged"
    exit 0
fi

# xHCI interrupts land on CPU0 on the Jetson. Keep IRQ handling there and
# schedule receive protocol work on every other online core.
rps_mask_value=$(( (1 << cpu_count) - 2 ))
printf -v rps_mask '%x' "${rps_mask_value}"

timeout_sec="${INSIGHT_NETWORK_TUNING_TIMEOUT:-180}"
deadline=$(( SECONDS + timeout_sec ))
expected_interfaces="${INSIGHT_CAMERA_INTERFACE_COUNT:-}"
if [[ -z "${expected_interfaces}" && -f config/cameras.json ]]; then
    expected_interfaces="$(python3 -c 'import json; print(sum(bool(camera.get("enabled", True)) for camera in json.load(open("config/cameras.json"))["cameras"]))' 2>/dev/null || true)"
fi
if [[ ! "${expected_interfaces}" =~ ^[1-9][0-9]*$ ]]; then
    expected_interfaces=1
fi
camera_interfaces=()
while (( SECONDS < deadline )); do
    camera_interfaces=()
    for interface_path in /sys/class/net/enx*; do
        [[ -e "${interface_path}" ]] || continue
        driver_path="$(readlink -f "${interface_path}/device/driver" 2>/dev/null || true)"
        [[ "${driver_path##*/}" == "cdc_ncm" ]] || continue
        [[ -w "${interface_path}/queues/rx-0/rps_cpus" ]] || continue
        camera_interfaces+=("${interface_path}")
    done
    (( ${#camera_interfaces[@]} >= expected_interfaces )) && break
    sleep 1
done

if (( ${#camera_interfaces[@]} == 0 )); then
    log "WARNING: no writable cdc_ncm enx* interfaces found within ${timeout_sec}s"
    exit 0
fi
if (( ${#camera_interfaces[@]} < expected_interfaces )); then
    log "WARNING: found ${#camera_interfaces[@]}/${expected_interfaces} expected camera interfaces before timeout"
fi

for interface_path in "${camera_interfaces[@]}"; do
    printf '%s\n' "${rps_mask}" > "${interface_path}/queues/rx-0/rps_cpus"
    if [[ -w "${interface_path}/queues/rx-0/rps_flow_cnt" ]]; then
        # The kernel rounds this table to a supported power of two.
        printf '%s\n' 8192 > "${interface_path}/queues/rx-0/rps_flow_cnt"
    fi
    log "${interface_path##*/}: rps_cpus=${rps_mask}"
done

log "configured ${#camera_interfaces[@]} camera interface(s)"
