#!/usr/bin/env bash
# Start (or ensure running) the Insight dashboard backend, then stay in the
# foreground so Ctrl-C can stop it cleanly. Default mode waits quietly (no
# log spam); --logs follows the backend logs instead; --jetson launches the
# on-device PyQt5 kiosk window. The two flags combine (--jetson --logs): the
# kiosk launches in the background and the backend logs (including the
# perf_tracker CPU breakdown, see scripts/perf_tracker.py) stream in this
# same terminal.
#
# Run from the host, this manages the container's whole lifecycle via
# docker compose. Run from inside the container (e.g. already `docker exec`'d
# in, or a devcontainer shell), it skips compose entirely and just waits on
# the backend that's already running, launching the kiosk directly in-process
# -- Ctrl-C only stops what this script started, not the backend. --logs
# isn't available in-container (no docker CLI to read the container's own
# stdout from inside itself).
#
# Usage:
#   ./scripts/run_dashboard.sh                    # quiet; Ctrl-C stops the backend
#   ./scripts/run_dashboard.sh --logs             # same, but follows backend logs
#   ./scripts/run_dashboard.sh --jetson           # also pull up the local kiosk
#                                                  # window (only if a monitor is
#                                                  # attached to this machine);
#                                                  # Ctrl-C closes the kiosk window
#                                                  # (backend keeps running --
#                                                  # `docker compose down` to stop it,
#                                                  # from the host)
#   ./scripts/run_dashboard.sh --jetson --logs    # both: kiosk runs in the
#                                                  # background, backend logs
#                                                  # stream here; Ctrl-C stops
#                                                  # both (and the backend)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PORT="${DASHBOARD_PORT:-8765}"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

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
    recording_in_progress=false
    if curl -sf "http://localhost:${PORT}/api/recording/status" 2>/dev/null \
            | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("recording") else 1)' 2>/dev/null; then
        recording_in_progress=true
    fi
    if [[ "${recording_in_progress}" == "true" ]]; then
        log "Backend is already running with a recording in progress -- not restarting (would kill it). Using the running backend as-is."
    else
        log "Backend is already running -- restarting for a clean launch..."
        docker compose restart
    fi
else
    log "Starting dashboard backend via docker compose..."
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

any_camera_live() {
    curl -sf "http://localhost:${PORT}/api/cameras" 2>/dev/null \
        | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if any(not c.get("stale", True) for c in d.get("cameras", [])) else 1)' \
        2>/dev/null
}

log "Waiting for backend to become healthy on :${PORT}..."
wait_for_backend_health
log "Backend is up."

# /healthz only proves the HTTP server is listening -- ROS2 discovery still
# needs a few seconds to actually start receiving camera/pose data after
# that. Launching --jetson (or opening the page) before real data is
# flowing showed an empty 3D view / no image stream even though everything
# was otherwise fine a few seconds later. Wait here instead of leaving that
# race to whoever's watching the screen.
log "Waiting for at least one camera to report live data..."
data_deadline=$(( $(date +%s) + 30 ))
stale_restart_at=$(( $(date +%s) + 10 ))
restarted_for_stale_dds=false
until any_camera_live; do
    now=$(date +%s)
    # Fast DDS enumerates network interfaces only when the participant is
    # created. The container auto-starts at host boot (restart:
    # unless-stopped), usually *before* the per-camera USB-ethernet links
    # exist, so the backend advertises unicast locators the cameras can't
    # route to (the WiFi IP instead of 169.254.x.2) and never receives a
    # single message. This does NOT self-heal: observed fully stale >15min
    # while a fresh `ros2 topic list` inside the same container saw every
    # topic instantly. Camera links up + still stale after 10s is exactly
    # that state, so restart the backend once -- the recreated participant
    # binds the now-present links and goes live within seconds.
    if [[ "${restarted_for_stale_dds}" == "false" && "${in_container}" == "false" ]] \
            && (( now >= stale_restart_at )) && camera_links_present; then
        log "Camera links are up but no data after 10s -- backend likely started before the camera links existed (stale DDS participant). Restarting backend once..."
        docker compose restart
        restarted_for_stale_dds=true
        wait_for_backend_health
        data_deadline=$(( $(date +%s) + 30 ))
        continue
    fi
    if (( now > data_deadline )); then
        log "WARNING: no camera reported live data within the wait window -- continuing anyway (check cameras/network if this is unexpected)."
        break
    fi
    sleep 1
done

if [[ "${jetson_mode}" == "true" ]]; then
    log "Launching on-device kiosk window..."
    export DISPLAY="${DISPLAY:-:0}"
    if [[ "${in_container}" == "true" ]]; then
        # Already inside the image that has PyQt5/QtWebEngine -- run the
        # kiosk directly instead of hopping back out through `docker exec`.
        # xhost (host-side X access control) isn't installed in this image
        # and isn't needed here: if you can already reach this shell with a
        # working DISPLAY, the host already granted access.
        # --logs was already rejected in-container above, so this is always
        # the last thing this script does when in_container.
        exec "${SCRIPT_DIR}/open_web_3d_right.sh"
    fi
    # The container connects to the host's X server as root, which the X
    # server's access control will reject by default unless the host
    # explicitly allows it. Harmless no-op if xhost isn't installed or
    # this DISPLAY has no server (`|| true` keeps `set -e` from tripping).
    xhost +SI:localuser:root >/dev/null 2>&1 || true
    # PyQt5/QtWebEngine only exist inside the image, not necessarily on a
    # fresh host, so this runs via `docker exec` rather than directly here.
    # -e DISPLAY overrides whatever was baked in at `docker compose up`
    # time, in case this shell's X session differs (e.g. it was started
    # over SSH without X, and you're now running --jetson from a local
    # desktop session instead).
    if [[ "${logs_mode}" == "true" ]]; then
        # Can't exec here -- need control back to fall through to the log
        # tail below. -d (detached) instead of -it: the kiosk is a GUI app
        # talking to DISPLAY, not this terminal, so it doesn't need a tty.
        docker exec -d -e DISPLAY="${DISPLAY}" insight-dashboard \
            /workspaces/insight_capture/scripts/open_web_3d_right.sh
        log "Kiosk launched in the background; following backend logs below."
    else
        exec docker exec -it -e DISPLAY="${DISPLAY}" insight-dashboard \
            /workspaces/insight_capture/scripts/open_web_3d_right.sh
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

# Stay in the foreground so Ctrl-C has something to interrupt. On the host
# this actually tears the backend down, instead of `up -d` leaving it running
# detached with no way to stop it from this script; inside the container
# there's no compose to tear down (and it keeps running the image's own main
# process either way), so Ctrl-C here just stops watching. The blocking
# command runs backgrounded + `wait`ed rather than plain foreground: bash
# only runs traps between commands, and a foreground external command can
# otherwise delay signal handling until *it* exits (which "sleep infinity"
# never does).
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
