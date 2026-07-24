#!/usr/bin/env bash
# Configure a Jetson host, build the image, and optionally start the dashboard.
#
# Usage:
#   ./scripts/setup_host.sh [--device jetson-nx] [--no-start]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

log()  { echo "[setup] $*"; }
fail() { echo "[setup] ERROR: $*" >&2; exit 1; }

no_start=false
device=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-start) no_start=true; shift ;;
        --device) device="${2:?--device requires a name}"; shift 2 ;;
        *) echo "Usage: $0 [--device <name>] [--no-start]" >&2; exit 1 ;;
    esac
done

[[ -f /.dockerenv ]] && fail "run this on the host, not inside the container."

# ── 0. device config profile ─────────────────────────────────────────────────
if [[ -n "${device}" ]]; then
    "${SCRIPT_DIR}/select_device.sh" "${device}"
elif [[ ! -f "${ROOT_DIR}/config/.device" ]]; then
    fail "no device profile selected yet -- run ./scripts/select_device.sh <name> first (or pass --device <name> here)."
fi
log "device profile: $(cat "${ROOT_DIR}/config/.device")"

# ── 1. docker + NVIDIA runtime ──────────────────────────────────────────────
command -v docker >/dev/null || fail "docker is not installed (install JetPack's docker or docker-ce)."
docker info >/dev/null 2>&1 || fail "docker daemon unreachable (is the service running? are you in the docker group?)."

if ! docker info 2>/dev/null | grep -q "Runtimes:.*nvidia"; then
    fail "NVIDIA container runtime not registered with docker.
  Install/repair with:  sudo apt install nvidia-container-toolkit && sudo systemctl restart docker
  (On JetPack it ships preinstalled -- check /etc/docker/daemon.json for a \"nvidia\" runtime entry.)"
fi
log "docker + nvidia runtime: OK"

if ! grep -qs "libgstnvjpeg" /etc/nvidia-container-runtime/host-files-for-container.d/*.csv 2>/dev/null; then
    log "WARNING: NVIDIA runtime CSV does not list libgstnvjpeg.so -- hardware JPEG"
    log "         encode will fall back to CPU inside the container (dashboard still works)."
fi

# Shared host tuning is also bundled for customer deployments.
"${SCRIPT_DIR}/host_setup.sh"

# ── 4. build the dashboard image ────────────────────────────────────────────
log "building the dashboard image (first build downloads ~2GB, later runs are cached)..."
cd "${ROOT_DIR}"
docker compose build
log "image build: OK"

# ── 5. start ────────────────────────────────────────────────────────────────
if [[ "${no_start}" == "true" ]]; then
    log "setup complete (--no-start). Launch later with: ./scripts/run_dashboard.sh"
    exit 0
fi
log "setup complete -- starting the dashboard..."
exec "${SCRIPT_DIR}/run_dashboard.sh"
