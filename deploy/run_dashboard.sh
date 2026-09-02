#!/usr/bin/env bash
# Start the dashboard, optionally follow logs or launch the Jetson kiosk.
#
# Usage:
#   ./scripts/run_dashboard.sh [--jetson] [--logs]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PORT="${DASHBOARD_PORT:-8765}"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

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

    # Compose resolves bind sources before the container starts. Read its
    # effective interpolation environment without sourcing the local .env.
    compose_environment="$(docker compose config --environment)"
    configured_source="$(compose_environment_value INSIGHT_ROSBAG_HOST_DIR)"
    required_source="$(compose_environment_value INSIGHT_ROSBAG_REQUIRED_SOURCE)"
    [[ -n "${configured_source}" ]] || return 0

    reason=""
    if [[ -n "${required_source}" && ! -e "${required_source}" ]]; then
        # Avoid touching a systemd automount when its backing device is absent:
        # that lookup returns ENODEV and prevents Docker from creating the
        # container, before the application-level NVMe fallback can run.
        reason="required source ${required_source} is absent"
    elif [[ ! -d "${configured_source}" ]]; then
        reason="configured path ${configured_source} is unavailable"
    elif [[ -n "${required_source}" ]]; then
        mounted_source="$(
            timeout 3 findmnt -no SOURCE --target "${configured_source}" 2>/dev/null \
                || true
        )"
        if [[ "${mounted_source}" != "${required_source}" \
            && "${mounted_source}" != "${required_source}["* ]]; then
            reason="required source ${required_source} does not match ${mounted_source:-an unmounted path}"
        fi
    fi

    [[ -n "${reason}" ]] || return 0
    fallback_source="${ROOT_DIR}/rosbags"
    mkdir -p "${fallback_source}"
    export INSIGHT_ROSBAG_HOST_DIR="${fallback_source}"
    log "WARNING: recording USB unavailable (${reason}); starting with NVMe fallback ${fallback_source}."
}

recording_is_active() {
    curl -sf "http://localhost:${PORT}/api/recording/status" 2>/dev/null \
        | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("recording") else 1)' \
        2>/dev/null
}

# Standard Docker marker file -- present in every container, regardless of
# how it was started (compose, devcontainer, plain `docker run`).
in_container=false
[[ -f /.dockerenv ]] && in_container=true

jetson_mode=false
logs_mode=false
for arg in "$@"; do
    case "${arg}" in
        --jetson) jetson_mode=true ;;
        --logs) logs_mode=true ;;
        *)
            echo "Usage: $0 [--jetson] [--logs]" >&2
            exit 1
            ;;
    esac
done

if [[ "${in_container}" == "true" && "${logs_mode}" == "true" ]]; then
    echo "--logs reads the container's own stdout via 'docker compose logs'," >&2
    echo "which only works from the host (no docker CLI in this image). Run" >&2
    echo "'docker compose logs -f' from the host instead." >&2
    exit 1
fi

cd "${ROOT_DIR}"

if [[ "${in_container}" == "true" ]]; then
    log "Already inside the container -- skipping docker compose."
elif [[ -n "$(docker compose ps --status running --services 2>/dev/null)" ]]; then
    # Already running: restart for a guaranteed-clean launch (picks up any
    # git-pulled code, clears whatever state accumulated) -- unless a
    # recording is in flight, in which case restarting would kill it.
    if recording_is_active; then
        log "Backend is already running with a recording in progress -- not restarting (would kill it). Using the running backend as-is."
    else
        # Reconcile changed commands, mounts, and environment before reloading
        # the bind-mounted dashboard code. A plain restart preserves stale
        # container definitions after a Compose refactor.
        log "Backend is already running -- reconciling Compose and restarting the dashboard..."
        prepare_recording_bind_source
        docker compose up -d
        docker compose restart insight-dashboard
    fi
else
    log "Starting dashboard backend via docker compose..."
    prepare_recording_bind_source
    docker compose up -d
fi

wait_for_backend_health() {
    local deadline=$(( $(date +%s) + 60 ))
    until curl -sf "http://localhost:${PORT}/healthz" >/dev/null 2>&1; do
        if (( $(date +%s) > deadline )); then
            log "ERROR: backend did not come up within 60s. Check: docker compose logs -f"
            exit 1
        fi
        sleep 1
    done
}

# At least one up interface carrying a 169.254.x.x address -- the per-camera
# point-to-point USB-ethernet links (see scripts/reboot_cameras.sh). Present
# means a camera is physically connected, whether or not data is flowing yet.
camera_links_present() {
    ip -4 -o addr show up 2>/dev/null \
        | awk '$2 != "lo" && $2 !~ /^docker/ && $4 ~ /^169\.254\./ {found=1} END {exit !found}'
}

all_cameras_live() {
    curl -sf "http://localhost:${PORT}/api/cameras" 2>/dev/null \
        | python3 -c '
import json, sys
cameras = json.load(sys.stdin).get("cameras", [])
by_name = {str(item.get("name")): item for item in cameras}
raw_only = all(
    by_name.get(name, {}).get("stale", True)
    and by_name.get(name, {}).get("native_vio_fresh", False)
    for name in ("insight3_a", "insight3_b")
)
healthy = cameras and all(
    not item.get("stale", True)
    or (raw_only and item.get("name") in {"insight3_a", "insight3_b"})
    for item in cameras
)
sys.exit(0 if healthy else 1)
' \
        2>/dev/null
}

stale_camera_names() {
    curl -sf "http://localhost:${PORT}/api/cameras" 2>/dev/null \
        | python3 -c 'import json,sys; print(", ".join(x["name"] for x in json.load(sys.stdin).get("cameras", []) if x.get("stale", True)) or "(none)")' \
        2>/dev/null || echo "(api unreachable)"
}

log "Waiting for backend to become healthy on :${PORT}..."
wait_for_backend_health
log "Backend is up."

# Wait for ROS data; restarting recreates DDS after late USB links appear.
ALL_LIVE_WAIT_SEC="${INSIGHT_ALL_LIVE_WAIT_SEC:-30}"
ALL_LIVE_MAX_RESTARTS="${INSIGHT_ALL_LIVE_MAX_RESTARTS:-3}"
log "Waiting for all cameras to report live data..."
data_restarts=0
while true; do
    data_deadline=$(( $(date +%s) + ALL_LIVE_WAIT_SEC ))
    all_live=false
    next_progress=$(( $(date +%s) + 5 ))
    while (( $(date +%s) <= data_deadline )); do
        if all_cameras_live; then
            all_live=true
            break
        fi
        if (( $(date +%s) >= next_progress )); then
            log "  still waiting, stale: $(stale_camera_names)"
            next_progress=$(( $(date +%s) + 5 ))
        fi
        sleep 1
    done
    if [[ "${all_live}" == "true" ]]; then
        log "All cameras are live."
        break
    fi
    if ! camera_links_present; then
        # No camera USB-ethernet link exists at all (e.g. a dev machine
        # with no cameras attached) -- restarting can't conjure data.
        log "WARNING: no camera links present and not all cameras live (stale: $(stale_camera_names)) -- continuing anyway."
        break
    fi
    if [[ "${in_container}" == "true" ]]; then
        log "WARNING: not all cameras live (stale: $(stale_camera_names)) and no docker CLI in-container to restart the backend -- continuing anyway."
        break
    fi
    if (( data_restarts >= ALL_LIVE_MAX_RESTARTS )); then
        log "WARNING: not all cameras live after ${ALL_LIVE_MAX_RESTARTS} backend restart(s) (stale: $(stale_camera_names)) -- continuing anyway."
        log "         If a stale camera answers HTTP but its interface shows no traffic, its stream is wedged -- reboot it: curl -X POST http://<camera-ip>/api/reboot"
        break
    fi
    if recording_is_active; then
        log "A recording started while waiting for camera data -- not restarting the backend."
        break
    fi
    data_restarts=$(( data_restarts + 1 ))
    log "Not all cameras live within ${ALL_LIVE_WAIT_SEC}s (stale: $(stale_camera_names)) -- restarting backend to recreate the DDS participant (attempt ${data_restarts}/${ALL_LIVE_MAX_RESTARTS})..."
    docker compose restart insight-dashboard
    wait_for_backend_health
done

if [[ "${jetson_mode}" == "true" ]]; then
    log "Launching on-device kiosk window..."
    export DISPLAY="${DISPLAY:-:0}"
    if [[ "${in_container}" == "true" ]]; then
        # Run the bundled kiosk directly when already inside the container.
        exec "${SCRIPT_DIR}/kiosk/open_web_3d_right.sh"
    fi
    # Grant the container access to the host X server.
    xhost +SI:localuser:root >/dev/null 2>&1 || true
    # Firefox runs as the unprivileged kiosk user.
    xhost +SI:localuser:"$(id -un)" >/dev/null 2>&1 || true
    # Run the bundled browser with the current desktop's DISPLAY.
    if [[ "${logs_mode}" == "true" ]]; then
        # Detach the GUI so this shell can continue following logs.
        docker exec -d -e DISPLAY="${DISPLAY}" insight-dashboard \
            /workspaces/insight_capture/deploy/kiosk/open_web_3d_right.sh
        log "Kiosk launched in the background; following backend logs below."
    else
        exec docker exec -it -e DISPLAY="${DISPLAY}" insight-dashboard \
            /workspaces/insight_capture/deploy/kiosk/open_web_3d_right.sh
    fi
fi

host_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

Dashboard backend is running on this machine's port ${PORT}.

From your own laptop, open an SSH tunnel and browse locally:
  ssh -L ${PORT}:localhost:${PORT} $(whoami)@${host_ip:-<this-jetson-ip>}
  then open http://localhost:${PORT}/ in your browser

(Pass --jetson to launch the on-device kiosk window here, --logs to follow
backend logs -- including the perf_tracker CPU breakdown -- instead of
running quietly, or both together.)

EOF
if [[ "${in_container}" == "true" ]]; then
    echo "Press Ctrl-C to stop watching (backend keeps running)."
else
    echo "Press Ctrl-C to stop the backend."
fi

# Background the waiter so Bash handles Ctrl-C traps promptly.
if [[ "${in_container}" == "true" ]]; then
    trap 'echo; log "Ctrl-C received, exiting (backend keeps running)."; exit 0' INT TERM
else
    trap 'echo; log "Ctrl-C received, stopping backend (docker compose down)..."; docker compose down; exit 0' INT TERM
fi

if [[ "${logs_mode}" == "true" ]]; then
    docker compose logs -f &
else
    sleep infinity &
fi
wait $!
