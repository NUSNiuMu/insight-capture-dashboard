#!/usr/bin/env bash
# Shared service selection for source and image deployments.

load_device_services() {
    local settings
    settings="$(python3 - "${1:-${ROOT_DIR}/config/cameras.json}" <<'PY'
import json, sys
with open(sys.argv[1]) as stream:
    config = json.load(stream)
domain = int(config["ros_domain_id"])
print(domain)
if any(c.get("enabled", True) and int(c.get("ros_domain_id", domain)) != domain
       for c in config["cameras"]):
    print("device-domain-relay")
if config.get("egohalo_rectification"):
    print("egohalo-stereo-rectifier")
if config.get("recording_h264", {}).get("input_topics"):
    print("recording-h264-encoder")
PY
)" || return
    local values
    mapfile -t values <<< "${settings}"
    export ROS_DOMAIN_ID="${values[0]}"
    optional_device_services=(device-domain-relay egohalo-stereo-rectifier recording-h264-encoder)
    selected_device_services=("${values[@]:1}")
    device_services=(superglue-inference insight9-sparse-mapper
        "${selected_device_services[@]}" insight3-global-localizer insight-dashboard)
}

read_capture_state() {
    local running response
    capture_busy=false
    running="$(docker compose ps --status running --services insight-dashboard)" || return
    [[ -n "${running}" ]] || return 0
    response="$(curl -fsS --max-time 15 "http://localhost:${PORT}/api/mobile/status")" || return
    capture_busy="$(python3 -c '
import json, sys
s = json.load(sys.stdin)
busy = s["recording"] or s["state"] in {"starting", "saving"}
busy = busy or (s.get("operation") or {}).get("state") == "running"
print("true" if busy else "false")
' <<< "${response}")"
}

require_capture_idle() {
    read_capture_state || return
    if [[ "${capture_busy}" == true ]]; then
        echo "ERROR: recording or saving is in progress; finish it before changing services." >&2
        return 1
    fi
}

compose_environment_value() {
    local name="$1"
    awk -v name="${name}" '
        index($0, name "=") == 1 {
            print substr($0, length(name) + 2)
            exit
        }
    ' <<< "${compose_environment}"
}

prepare_recording_bind_source() {
    local configured_source required_source mounted_source fallback_source reason
    # Preserve the existing visible USB/NVMe storage policy at every entry point.
    compose_environment="$(docker compose config --environment)"
    configured_source="$(compose_environment_value INSIGHT_ROSBAG_HOST_DIR)"
    required_source="$(compose_environment_value INSIGHT_ROSBAG_REQUIRED_SOURCE)"
    [[ -n "${configured_source}" ]] || return 0
    reason=""
    if [[ -n "${required_source}" && ! -e "${required_source}" ]]; then
        reason="required source ${required_source} is absent"
    elif [[ ! -d "${configured_source}" ]]; then
        reason="configured path ${configured_source} is unavailable"
    elif [[ -n "${required_source}" ]]; then
        mounted_source="$(timeout 3 findmnt -no SOURCE --target "${configured_source}" 2>/dev/null || true)"
        if [[ "${mounted_source}" != "${required_source}" && "${mounted_source}" != "${required_source}["* ]]; then
            reason="required source ${required_source} does not match ${mounted_source:-an unmounted path}"
        fi
    fi
    [[ -n "${reason}" ]] || return 0
    fallback_source="${ROOT_DIR}/rosbags"
    mkdir -p "${fallback_source}"
    export INSIGHT_ROSBAG_HOST_DIR="${fallback_source}"
    log "WARNING: recording USB unavailable (${reason}); starting with NVMe fallback ${fallback_source}."
}

start_device_services() {
    load_device_services
    prepare_recording_bind_source
    local service inactive=()
    for service in "${optional_device_services[@]}"; do
        if [[ " ${selected_device_services[*]} " != *" ${service} "* ]]; then
            inactive+=("${service}")
        fi
    done
    if (( ${#inactive[@]} )); then
        docker compose stop "${inactive[@]}"
    fi
    # Explicit service names activate optional Compose services without a second profile switch.
    docker compose up -d "${device_services[@]}"
}

stop_device_services() {
    require_capture_idle
    load_device_services
    docker compose stop insight-dashboard insight3-global-localizer insight9-sparse-mapper \
        "${optional_device_services[@]}" superglue-inference
}

wait_for_backend_health() {
    local deadline=$(( $(date +%s) + 60 ))
    until curl -sf --max-time 3 "http://localhost:${PORT}/healthz" >/dev/null 2>&1; do
        if (( $(date +%s) > deadline )); then
            echo "ERROR: backend did not come up within 60s. Check: docker compose logs -f" >&2
            return 1
        fi
        sleep 1
    done
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    set -euo pipefail
    ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    PORT="${DASHBOARD_PORT:-8765}"
    log() { echo "$*"; }
    cd "${ROOT_DIR}"
    case "${1:-}" in
        start) require_capture_idle; start_device_services; wait_for_backend_health ;;
        stop) stop_device_services ;;
        *) echo "Usage: $0 <start|stop>" >&2; exit 1 ;;
    esac
fi
