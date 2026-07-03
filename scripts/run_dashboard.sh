#!/usr/bin/env bash
# Start (or ensure running) the Insight dashboard backend via docker compose,
# then either hand back SSH-tunnel instructions for remote/laptop viewing
# (default) or launch the on-device PyQt5 kiosk window (--jetson).
#
# Usage:
#   ./scripts/run_dashboard.sh            # backend only; view from your own
#                                          # laptop over an SSH tunnel
#   ./scripts/run_dashboard.sh --jetson   # also pull up the local kiosk
#                                          # window (only if a monitor is
#                                          # attached to this machine)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PORT="${DASHBOARD_PORT:-8765}"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

mode="${1:-}"
if [[ -n "${mode}" && "${mode}" != "--jetson" ]]; then
    echo "Usage: $0 [--jetson]" >&2
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

if [[ "${mode}" == "--jetson" ]]; then
    log "Launching on-device kiosk window (inside the container)..."
    # PyQt5/QtWebEngine only exist inside the image, not necessarily on a
    # fresh host, so this runs via `docker exec` rather than directly here.
    # -e DISPLAY overrides whatever was baked in at `docker compose up`
    # time, in case this shell's X session differs (e.g. it was started
    # over SSH without X, and you're now running --jetson from a local
    # desktop session instead).
    exec docker exec -it -e DISPLAY="${DISPLAY:-:0}" insight-dashboard \
        /workspaces/insight_capture/scripts/open_web_3d_right.sh
fi

host_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

Dashboard backend is running on this machine's port ${PORT}.

From your own laptop, open an SSH tunnel and browse locally:
  ssh -L ${PORT}:localhost:${PORT} $(whoami)@${host_ip:-<this-jetson-ip>}
  then open http://localhost:${PORT}/ in your browser

(Pass --jetson instead to launch the on-device kiosk window here.)
EOF
