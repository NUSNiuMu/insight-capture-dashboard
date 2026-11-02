#!/usr/bin/env bash
# Start capture services; the native Qt desktop is a separate host process.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PORT="${DASHBOARD_PORT:-8765}"
log() { echo "$*"; }
source "${SCRIPT_DIR}/device_services.sh"
logs_mode=false
if [[ "${1:-}" == --logs && $# == 1 ]]; then
    logs_mode=true
elif (( $# != 0 )); then
    echo "Usage: $0 [--logs]" >&2
    exit 1
fi
cd "${ROOT_DIR}"
read_capture_state
if [[ "${capture_busy}" == true ]]; then
    log "Capture is active; keeping the running services."
else
    running="$(docker compose ps --status running --services insight-dashboard)"
    start_device_services
    if [[ -n "${running}" ]]; then
        docker compose restart insight-dashboard
    fi
fi
wait_for_backend_health
host_ip="${DASHBOARD_ADVERTISE_HOST:-<Jetson-IP>}"
printf 'Phone: http://%s:%s/mobile\nDesktop: ./native_dashboard/run.sh\n' "${host_ip}" "${PORT}"
if [[ "${logs_mode}" == true ]]; then
    exec docker compose logs -f
fi
