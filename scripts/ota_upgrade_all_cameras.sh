#!/usr/bin/env bash
# Batch OTA-upgrade every Insight camera currently connected to this host
# (the two insight3_* infrared cameras + insight9_a on this device, or
# whatever set is physically plugged in at run time).
#
# looper_cli.py's own upgrade/current/list commands only ever target ONE
# device per invocation (--device-base-url, or its own single-endpoint
# auto-detect if omitted -- see looper_cli/looper_cli/device.py's
# resolve_device_base_url, which explicitly warns it isn't safe to rely on
# with more than one camera attached). This script is the fan-out: discover
# every camera's IP the same way scripts/reboot_cameras.sh already does
# (each camera sits on its own dedicated 169.254.x.x point-to-point link,
# so devices are found by inspecting which local interfaces currently carry
# an address in that range -- not a brute-force /16 scan), then invoke
# looper_cli.py once per discovered device.
#
# NOT executed as part of writing this script -- run manually when ready:
#   ./scripts/ota_upgrade_all_cameras.sh                # list only (current versions), no changes
#   ./scripts/ota_upgrade_all_cameras.sh --upgrade       # actually upgrade every discovered camera
#   ./scripts/ota_upgrade_all_cameras.sh --upgrade --version 1.2.3   # pin a version instead of --latest
#
# Cameras are upgraded ONE AT A TIME, not in parallel: an OTA reboots the
# camera, and rebooting all three simultaneously would drop every camera
# link at once (recording/live view goes dark fleet-wide) instead of just
# the one currently mid-upgrade. Each device gets its own log line; one
# camera's failure does not abort the rest of the batch (summarized at the
# end instead).

set -uo pipefail  # NOT -e: one camera's failure must not kill the loop

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI="python3 ${SCRIPT_DIR}/../looper_cli/looper_cli.py"

DISCOVERY_TIMEOUT="${INSIGHT_DISCOVERY_TIMEOUT:-40}"
DISCOVERY_INTERVAL=2

do_upgrade=false
target_version=""
watch_seconds=60
for arg in "$@"; do
    case "${arg}" in
        --upgrade) do_upgrade=true ;;
        --version=*) target_version="${arg#--version=}" ;;
        --version)
            echo "Usage: --version=X.Y.Z (use = form)" >&2; exit 1 ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown argument: ${arg}" >&2; exit 1 ;;
    esac
done

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# Identical discovery convention to scripts/reboot_cameras.sh: each camera
# is the ".1" or ".2" peer on its own 169.254.x.x/24 point-to-point link.
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
    echo "ERROR: no cameras found on any 169.254.x.x interface within ${DISCOVERY_TIMEOUT}s." >&2
    exit 1
fi

log "Found ${#DEVICES[@]} camera(s): ${DEVICES[*]}"

if [[ "${do_upgrade}" != "true" ]]; then
    log "List-only mode (pass --upgrade to actually update). Current versions:"
    for device in "${DEVICES[@]}"; do
        ${CLI} --device-base-url "${device}" current || log "  ${device}: unreachable/error"
    done
    exit 0
fi

version_args=(--latest)
[[ -n "${target_version}" ]] && version_args=(--version "${target_version}")

declare -A RESULTS
for device in "${DEVICES[@]}"; do
    log "=== Upgrading ${device} (${version_args[*]}) ==="
    if ${CLI} --device-base-url "${device}" ota upgrade "${version_args[@]}" \
            --watch-seconds "${watch_seconds}" -y; then
        RESULTS["${device}"]="ok"
    else
        RESULTS["${device}"]="FAILED"
        log "WARNING: ${device} upgrade reported failure -- continuing with remaining cameras"
    fi
done

echo
log "Summary:"
for device in "${DEVICES[@]}"; do
    printf "  %-24s %s\n" "${device}" "${RESULTS[${device}]:-unknown}"
done

if [[ " ${RESULTS[*]} " == *"FAILED"* ]]; then
    exit 1
fi
