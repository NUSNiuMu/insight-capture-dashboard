#!/usr/bin/env bash
# Activate config/devices/<name> as this checkout's live device profile.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
DEVICES_DIR="${ROOT_DIR}/config/devices"

device="${1:?usage: $0 <device-name> (available: $(ls "$DEVICES_DIR" 2>/dev/null | tr '\n' ' '))}"
profile_dir="${DEVICES_DIR}/${device}"

if [[ ! -d "$profile_dir" ]]; then
    echo "no such device profile: '${device}' (available: $(ls "$DEVICES_DIR" | tr '\n' ' '))" >&2
    exit 1
fi

cp "${profile_dir}/cameras.json" "${ROOT_DIR}/config/cameras.json"
cp "${profile_dir}/board_calibration.json" "${ROOT_DIR}/config/board_calibration.json"
cp "${profile_dir}/post_processing.json" "${ROOT_DIR}/config/post_processing.json"
echo "$device" > "${ROOT_DIR}/config/.device"

echo "selected device profile: ${device}"
