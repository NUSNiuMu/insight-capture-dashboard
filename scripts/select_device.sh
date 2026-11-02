#!/usr/bin/env bash
# Activate a device profile and reload its services; --no-start prepares an offline installation.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
DEVICES_DIR="${ROOT_DIR}/config/devices"

device="${1:?usage: $0 <device-name> (available: $(ls "$DEVICES_DIR" 2>/dev/null | tr '\n' ' '))}"
profile_dir="${DEVICES_DIR}/${device}"
no_start=false
if [[ "${2:-}" == --no-start && $# == 2 ]]; then
    no_start=true
elif (( $# != 1 )); then
    echo "Usage: $0 <device-name> [--no-start]" >&2
    exit 1
fi

if [[ ! -d "$profile_dir" ]]; then
    echo "no such device profile: '${device}' (available: $(ls "$DEVICES_DIR" | tr '\n' ' '))" >&2
    exit 1
fi

PORT="${DASHBOARD_PORT:-8765}"
log() { echo "$*"; }
source "${ROOT_DIR}/deploy/device_services.sh"
cd "${ROOT_DIR}"
load_device_services "${profile_dir}/cameras.json"
python3 -m json.tool "${profile_dir}/runtime.json" >/dev/null
if [[ "${no_start}" == false ]]; then
    require_capture_idle
    prepare_recording_bind_source
    # Stop all config readers before replacing the live files. Keep inference engines warm.
    docker compose stop insight-dashboard insight3-global-localizer insight9-sparse-mapper \
        "${optional_device_services[@]}"
fi

cp "${profile_dir}/cameras.json" "${ROOT_DIR}/config/cameras.json"
cp "${profile_dir}/runtime.json" "${ROOT_DIR}/config/runtime.json"
echo "$device" > "${ROOT_DIR}/config/.device"

echo "selected device profile: ${device}"
if [[ "${no_start}" == false ]]; then
    start_device_services
    wait_for_backend_health
    echo "device services ready; phone page: http://<jetson-ip>:${PORT}/mobile"
fi
