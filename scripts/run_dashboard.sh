#!/usr/bin/env bash
# Start (or ensure running) the Insight dashboard backend, then stay in the
# foreground so Ctrl-C can stop it cleanly. Default mode waits quietly (no
# log spam); --logs follows the backend logs instead; --jetson launches the
# on-device PyQt5 kiosk window.
#
# Run from the host, this manages the container's whole lifecycle via
# docker compose. Run from inside the container (e.g. already `docker exec`'d
# in, or a devcontainer shell), it skips compose entirely and just waits on
# the backend that's already running, launching the kiosk directly in-process
# -- Ctrl-C only stops what this script started, not the backend.
#
# Usage:
#   ./scripts/run_dashboard.sh            # quiet; Ctrl-C stops the backend
#   ./scripts/run_dashboard.sh --logs     # same, but follows backend logs
#   ./scripts/run_dashboard.sh --jetson   # also pull up the local kiosk
#                                          # window (only if a monitor is
#                                          # attached to this machine);
#                                          # Ctrl-C closes the kiosk window
#                                          # (backend keeps running --
#                                          # `docker compose down` to stop it,
#                                          # from the host)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PORT="${DASHBOARD_PORT:-8765}"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# Standard Docker marker file -- present in every container, regardless of
# how it was started (compose, devcontainer, plain `docker run`).
in_container=false
[[ -f /.dockerenv ]] && in_container=true

mode="${1:-}"
if [[ -n "${mode}" && "${mode}" != "--jetson" && "${mode}" != "--logs" ]]; then
    echo "Usage: $0 [--jetson|--logs]" >&2
    exit 1
fi

if [[ "${in_container}" == "true" && "${mode}" == "--logs" ]]; then
    echo "--logs reads the container's own stdout via 'docker compose logs'," >&2
    echo "which only works from the host (no docker CLI in this image). Run" >&2
    echo "'docker compose logs -f' from the host instead." >&2
    exit 1
fi

cd "${ROOT_DIR}"

if [[ "${in_container}" == "true" ]]; then
    log "Already inside the container -- skipping docker compose."
else
    log "Starting dashboard backend via docker compose..."
    docker compose up -d
fi

log "Waiting for backend to become healthy on :${PORT}..."
deadline=$(( $(date +%s) + 60 ))
until curl -sf "http://localhost:${PORT}/healthz" >/dev/null 2>&1; do
    if (( $(date +%s) > deadline )); then
        log "ERROR: backend did not come up within 60s. Check: docker compose logs -f"
        exit 1
    fi
    sleep 1
done
log "Backend is up."

# /healthz only proves the HTTP server is listening -- ROS2 discovery still
# needs a few seconds to actually start receiving camera/pose data after
# that. Launching --jetson (or opening the page) before real data is
# flowing showed an empty 3D view / no image stream even though everything
# was otherwise fine a few seconds later. Wait here instead of leaving that
# race to whoever's watching the screen.
log "Waiting for at least one camera to report live data..."
data_deadline=$(( $(date +%s) + 30 ))
until curl -sf "http://localhost:${PORT}/api/cameras" 2>/dev/null \
        | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if any(not c.get("stale", True) for c in d.get("cameras", [])) else 1)' \
        2>/dev/null; do
    if (( $(date +%s) > data_deadline )); then
        log "WARNING: no camera reported live data within 30s -- continuing anyway (check cameras/network if this is unexpected)."
        break
    fi
    sleep 1
done

if [[ "${mode}" == "--jetson" ]]; then
    log "Launching on-device kiosk window..."
    export DISPLAY="${DISPLAY:-:0}"
    if [[ "${in_container}" == "true" ]]; then
        # Already inside the image that has PyQt5/QtWebEngine -- run the
        # kiosk directly instead of hopping back out through `docker exec`.
        # xhost (host-side X access control) isn't installed in this image
        # and isn't needed here: if you can already reach this shell with a
        # working DISPLAY, the host already granted access.
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
    exec docker exec -it -e DISPLAY="${DISPLAY}" insight-dashboard \
        /workspaces/insight_capture/scripts/open_web_3d_right.sh
fi

host_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

Dashboard backend is running on this machine's port ${PORT}.

From your own laptop, open an SSH tunnel and browse locally:
  ssh -L ${PORT}:localhost:${PORT} $(whoami)@${host_ip:-<this-jetson-ip>}
  then open http://localhost:${PORT}/ in your browser

(Pass --jetson to launch the on-device kiosk window here, or --logs to
follow backend logs instead of running quietly.)

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

if [[ "${mode}" == "--logs" ]]; then
    docker compose logs -f &
else
    sleep infinity &
fi
wait $!
