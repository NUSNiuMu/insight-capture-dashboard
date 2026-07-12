#!/usr/bin/env bash
# One-time host setup for a fresh Jetson, then hand off to run_dashboard.sh.
# Idempotent: safe to re-run any time (e.g. after a JetPack reflash, or when
# diagnosing "it worked on the other device" differences).
#
# What it does, in order:
#   1. sanity-check docker + the NVIDIA container runtime (hardware JPEG
#      encode needs the runtime's GStreamer plugin injection)
#   2/3. sysctl DDS receive buffers + boot-time camera reboot unit + CPU
#      power mode check -- see scripts/host_setup.sh (shared with the
#      end-user deploy path, bundled there by build_release.sh)
#   4. docker compose build
#   5. ./scripts/run_dashboard.sh  (skipped with --no-start)
#
# Usage:
#   ./scripts/setup_host.sh              # full setup + start
#   ./scripts/setup_host.sh --no-start   # setup only (CI / pre-provisioning)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

log()  { echo "[setup] $*"; }
fail() { echo "[setup] ERROR: $*" >&2; exit 1; }

no_start=false
for arg in "$@"; do
    case "${arg}" in
        --no-start) no_start=true ;;
        *) echo "Usage: $0 [--no-start]" >&2; exit 1 ;;
    esac
done

[[ -f /.dockerenv ]] && fail "run this on the host, not inside the container."

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

# ── 2/3. sysctl DDS buffers + boot-time camera reboot unit + CPU power mode ──
# Shared with the end-user path (build_release.sh bundles this same file into
# the deploy package) so both paths get identical host tuning.
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
