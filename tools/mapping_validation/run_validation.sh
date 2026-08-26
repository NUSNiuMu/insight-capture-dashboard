#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

export DISPLAY="${DISPLAY:-:0}"
if [[ -z "${XAUTHORITY:-}" ]]; then
    SESSION_UID="$(id -u)"
    export XAUTHORITY="/run/user/${SESSION_UID}/gdm/Xauthority"
fi

if [[ ! -S /tmp/.X11-unix/X0 ]]; then
    echo "X11 display socket /tmp/.X11-unix/X0 is unavailable." >&2
    exit 1
fi
if [[ ! -r "${XAUTHORITY}" ]]; then
    echo "XAUTHORITY is not readable: ${XAUTHORITY}" >&2
    exit 1
fi

XHOST_GRANTED=0
MAPPER_DEBUG_ENABLED=0
LOCALIZER_DEBUG_ENABLED=0

call_ros_service() {
    docker compose exec -T insight9-sparse-mapper \
        /entrypoint.sh ros2 service call "$1" "$2" "$3"
}

wait_for_ros_service() {
    local service_name="$1"
    local service_type="$2"
    local attempt
    local discovered_type
    for attempt in $(seq 1 30); do
        discovered_type="$(
            docker compose exec -T insight9-sparse-mapper \
                /entrypoint.sh ros2 service type "${service_name}" 2>/dev/null || true
        )"
        if [[ "${discovered_type}" == "${service_type}" ]]; then
            return 0
        fi
        sleep 1
    done
    echo "ROS service is unavailable: ${service_name} (${service_type})" >&2
    return 1
}

cleanup() {
    local exit_status=$?
    local cleanup_failed=0
    trap - EXIT INT TERM
    if [[ "${LOCALIZER_DEBUG_ENABLED}" -eq 1 ]]; then
        call_ros_service \
            /insight_global/set_debug_topics \
            std_srvs/srv/SetBool \
            '{data: false}' >/dev/null || cleanup_failed=1
    fi
    if [[ "${MAPPER_DEBUG_ENABLED}" -eq 1 ]]; then
        call_ros_service \
            /insight9_sparse_map/set_debug_topics \
            std_srvs/srv/SetBool \
            '{data: false}' >/dev/null || cleanup_failed=1
    fi
    if [[ "${XHOST_GRANTED}" -eq 1 ]]; then
        xhost -si:localuser:root >/dev/null 2>&1 || true
    fi
    if [[ "${cleanup_failed}" -eq 1 ]]; then
        echo "Failed to disable one or more RViz debug topics." >&2
        if [[ "${exit_status}" -eq 0 ]]; then
            exit_status=1
        fi
    fi
    exit "${exit_status}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

xhost +si:localuser:root >/dev/null
XHOST_GRANTED=1

echo "Starting a new sparse localization session; mapping remains available in the web dashboard after RViz closes."
docker compose --profile mapping-validation up -d --wait --wait-timeout 900 \
    superglue-inference \
    insight9-sparse-mapper \
    insight3-global-localizer
docker compose --profile mapping-validation stop insight9-dense-mapper

wait_for_ros_service \
    /insight9_sparse_map/set_debug_topics \
    std_srvs/srv/SetBool
wait_for_ros_service \
    /insight_global/set_debug_topics \
    std_srvs/srv/SetBool

call_ros_service \
    /insight9_sparse_map/set_debug_topics \
    std_srvs/srv/SetBool \
    '{data: true}'
MAPPER_DEBUG_ENABLED=1
call_ros_service \
    /insight_global/set_debug_topics \
    std_srvs/srv/SetBool \
    '{data: true}'
LOCALIZER_DEBUG_ENABLED=1

call_ros_service \
    /insight9_sparse_map/reset \
    std_srvs/srv/Empty \
    '{}'
call_ros_service \
    /insight_global/reset \
    std_srvs/srv/Empty \
    '{}'

docker compose --profile mapping-validation run --rm --no-deps \
    insight9-mapping-rviz
