#!/usr/bin/env bash
# Start (or ensure running) the Insight dashboard backend via docker compose,
# then stay in the foreground so Ctrl-C can stop it cleanly. Default mode
# waits quietly (no log spam); --logs follows the backend logs instead;
# --jetson launches the on-device PyQt5 kiosk window.
#
# Usage:
#   ./scripts/run_dashboard.sh            # quiet; Ctrl-C stops the backend
#   ./scripts/run_dashboard.sh --logs     # same, but follows backend logs
#   ./scripts/run_dashboard.sh --jetson   # also pull up the local kiosk
#                                          # window (only if a monitor is
#                                          # attached to this machine);
#                                          # Ctrl-C closes the kiosk window
#                                          # (backend keeps running --
#                                          # `docker compose down` to stop it)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PORT="${DASHBOARD_PORT:-8765}"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

mode="${1:-}"
if [[ -n "${mode}" && "${mode}" != "--jetson" && "${mode}" != "--logs" ]]; then
    echo "Usage: $0 [--jetson|--logs]" >&2
    exit 1
fi

cd "${ROOT_DIR}"

log "Starting dashboard backend via docker compose..."
docker compose up -d

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
    log "Launching on-device kiosk window (inside the container)..."
    export DISPLAY="${DISPLAY:-:0}"
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

Press Ctrl-C to stop the backend.
EOF

# Stay in the foreground so Ctrl-C has something to interrupt and actually
# tears the backend down, instead of `up -d` leaving it running detached
# with no way to stop it from this script. The blocking command runs
# backgrounded + `wait`ed rather than plain foreground: bash only runs traps
# between commands, and a foreground external command can otherwise delay
# signal handling until *it* exits (which "sleep infinity" never does).
trap 'echo; log "Ctrl-C received, stopping backend (docker compose down)..."; docker compose down; exit 0' INT TERM

if [[ "${mode}" == "--logs" ]]; then
    docker compose logs -f &
else
    sleep infinity &
fi
wait $!
