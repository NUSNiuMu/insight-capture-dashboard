#!/usr/bin/env bash
# Install or upgrade the Insight dashboard from a docker image tarball.
# Runs on the CUSTOMER machine, from the deploy bundle directory.
#
# Usage:
#   ./update.sh insight-dashboard-v1.2.0.tar.gz            # install / upgrade
#   ./update.sh --rollback v1.1.0                          # switch back to an
#                                                          # already-loaded tag
#
# What it does:
#   1. docker load the tarball (skipped for --rollback)
#   2. First install only: seed config/ out of the image so the app's
#      editable settings/calibration live on the host and survive upgrades
#   3. Point .env's INSIGHT_VERSION at the new tag and `docker compose up -d`
#   4. Wait for the backend /healthz to come up and report the result
#
# Refuses to restart while a recording is in progress (override: --force).
# Old images are kept for rollback; clean up manually with `docker image ls
# insight-dashboard` + `docker rmi` when disk gets tight.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"
PORT="${DASHBOARD_PORT:-8765}"
IMAGE_NAME="insight-dashboard"

log() { echo "[$(date '+%H:%M:%S')] $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

force=false
rollback=false
tarball=""
rollback_version=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --force) force=true; shift ;;
        --rollback)
            rollback=true
            rollback_version="${2:?--rollback needs a version, e.g. --rollback v1.1.0}"
            shift 2 ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) die "unknown option: $1 (see --help)" ;;
        *) tarball="$1"; shift ;;
    esac
done

command -v docker >/dev/null || die "docker is not installed"
docker compose version >/dev/null 2>&1 || die "docker compose (v2 plugin) is not available"

# ── Don't kill an in-flight recording ────────────────────────────────────────
if curl -sf "http://localhost:${PORT}/api/recording/status" 2>/dev/null \
        | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("recording") else 1)' 2>/dev/null; then
    if [[ "${force}" == "true" ]]; then
        log "WARNING: a recording is in progress; --force given, proceeding anyway (the recording will be cut short)."
    else
        die "a recording is in progress -- finish it first, or re-run with --force to interrupt it"
    fi
fi

# ── Resolve the target version ───────────────────────────────────────────────
if [[ "${rollback}" == "true" ]]; then
    version="${rollback_version}"
    docker image inspect "${IMAGE_NAME}:${version}" >/dev/null 2>&1 \
        || die "image ${IMAGE_NAME}:${version} is not loaded on this machine (docker image ls ${IMAGE_NAME})"
    log "Rolling back to ${IMAGE_NAME}:${version}"
else
    [[ -n "${tarball}" ]] || die "usage: $0 <image-tarball.tar.gz>  (see --help)"
    [[ -f "${tarball}" ]] || die "file not found: ${tarball}"
    log "Loading image from ${tarball} (this can take a few minutes)..."
    load_output="$(docker load -i "${tarball}")"
    echo "${load_output}"
    # "Loaded image: insight-dashboard:v1.2.0" -> v1.2.0
    tag="$(sed -n "s/^Loaded image: ${IMAGE_NAME}://p" <<<"${load_output}" | head -1)"
    [[ -n "${tag}" ]] || die "tarball did not contain an ${IMAGE_NAME}:* image (got: ${load_output})"
    version="${tag}"
fi

# ── Data directories (persist across upgrades) ───────────────────────────────
mkdir -p rosbags outputs runs

# First install: seed config/ from the image. Never overwrite an existing
# config/ -- it holds this site's calibration and settings.
if [[ ! -d config ]]; then
    log "First install: seeding config/ from the image..."
    seed_ctr="$(docker create "${IMAGE_NAME}:${version}")"
    docker cp "${seed_ctr}:/workspaces/insight_capture/config" ./config
    docker rm "${seed_ctr}" >/dev/null
fi

# ── Record the previous version (for the rollback hint), switch, restart ────
prev_version="$(sed -n 's/^INSIGHT_VERSION=//p' .env 2>/dev/null | head -1 || true)"
echo "INSIGHT_VERSION=${version}" > .env

log "Starting ${IMAGE_NAME}:${version} ..."
docker compose up -d

log "Waiting for the backend to become healthy on :${PORT} ..."
deadline=$(( $(date +%s) + 90 ))
until curl -sf "http://localhost:${PORT}/healthz" >/dev/null 2>&1; do
    if (( $(date +%s) > deadline )); then
        echo "ERROR: backend did not come up within 90s. Check: docker compose logs -f" >&2
        if [[ -n "${prev_version}" && "${prev_version}" != "${version}" ]]; then
            echo "To roll back: ./update.sh --rollback ${prev_version}" >&2
        fi
        exit 1
    fi
    sleep 2
done

log "Done. Dashboard is running ${IMAGE_NAME}:${version} on port ${PORT}."
if [[ -n "${prev_version}" && "${prev_version}" != "${version}" ]]; then
    log "Previous version ${prev_version} is still loaded; roll back anytime with: ./update.sh --rollback ${prev_version}"
fi
