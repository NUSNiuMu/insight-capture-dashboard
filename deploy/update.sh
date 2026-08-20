#!/usr/bin/env bash
# Install, upgrade, or roll back a customer deployment.
#
# Usage:
#   ./update.sh insight-dashboard-v1.2.0.tar.gz
#   ./update.sh --rollback v1.1.0
#
# Refuses to restart while a recording is in progress (override: --force).
# Old images remain available for rollback.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"
PORT="${DASHBOARD_PORT:-8765}"
IMAGE_NAME="insight-dashboard"
SUPERGLUE_IMAGE="insight-superglue-validation:25.04"
SUPERGLUE_TARBALL="insight-superglue-validation-25.04.tar.gz"

if [[ -x "${SCRIPT_DIR}/deploy/install_voice_control_service.sh" ]]; then
    HOST_RUNTIME_ROOT="${SCRIPT_DIR}"
    VOICE_INSTALLER="${SCRIPT_DIR}/deploy/install_voice_control_service.sh"
else
    HOST_RUNTIME_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
    VOICE_INSTALLER="${SCRIPT_DIR}/install_voice_control_service.sh"
fi

log() { echo "[$(date '+%H:%M:%S')] $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

sync_host_voice_runtime() {
    local image="$1"
    local container temporary
    temporary="$(mktemp -d)"
    container="$(docker create "${image}")"
    cleanup_voice_sync() {
        docker rm "${container}" >/dev/null 2>&1 || true
        rm -rf "${temporary}"
    }
    trap cleanup_voice_sync EXIT

    docker cp "${container}:/workspaces/insight_capture/insight_capture/voice" "${temporary}/voice"
    docker cp "${container}:/workspaces/insight_capture/insight_capture/__init__.py" "${temporary}/insight_capture_init.py"
    docker cp "${container}:/workspaces/insight_capture/scripts/run_voice.sh" "${temporary}/run_voice.sh"
    docker cp "${container}:/workspaces/insight_capture/scripts/set_ros_domain_id.py" "${temporary}/set_ros_domain_id.py"
    docker cp "${container}:/workspaces/insight_capture/deploy/install_voice_control_service.sh" "${temporary}/install_voice_control_service.sh"
    docker cp "${container}:/workspaces/insight_capture/deploy/systemd/insight-voice-control.service.in" "${temporary}/insight-voice-control.service.in"
    docker cp "${container}:/workspaces/insight_capture/deploy/update.sh" "${temporary}/update.sh"

    mkdir -p \
        "${HOST_RUNTIME_ROOT}/insight_capture" \
        "${HOST_RUNTIME_ROOT}/scripts" \
        "${HOST_RUNTIME_ROOT}/deploy/systemd"
    rm -rf "${HOST_RUNTIME_ROOT}/insight_capture/voice"
    mv "${temporary}/voice" "${HOST_RUNTIME_ROOT}/insight_capture/voice"
    install -m 0644 "${temporary}/insight_capture_init.py" "${HOST_RUNTIME_ROOT}/insight_capture/__init__.py"
    install -m 0755 "${temporary}/run_voice.sh" "${HOST_RUNTIME_ROOT}/scripts/run_voice.sh"
    install -m 0755 "${temporary}/set_ros_domain_id.py" "${HOST_RUNTIME_ROOT}/scripts/set_ros_domain_id.py"
    install -m 0755 "${temporary}/install_voice_control_service.sh" "${HOST_RUNTIME_ROOT}/deploy/install_voice_control_service.sh"
    install -m 0644 "${temporary}/insight-voice-control.service.in" "${HOST_RUNTIME_ROOT}/deploy/systemd/insight-voice-control.service.in"
    install -m 0755 "${temporary}/update.sh" "${SCRIPT_DIR}/update.sh"

    trap - EXIT
    cleanup_voice_sync
}

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

# The stable mapping/relocalization dependency ships as a separate archive so
# routine dashboard upgrades do not resend it. First install auto-discovers the
# dependency beside the dashboard archive (or in this deployment directory).
if ! docker image inspect "${SUPERGLUE_IMAGE}" >/dev/null 2>&1; then
    dependency_dirs=("${SCRIPT_DIR}")
    if [[ -n "${tarball}" ]]; then
        tarball_dir="$(cd "$(dirname "${tarball}")" && pwd)"
        dependency_dirs=("${tarball_dir}" "${SCRIPT_DIR}")
    fi
    dependency_loaded=false
    for dependency_dir in "${dependency_dirs[@]}"; do
        dependency_tarball="${dependency_dir}/${SUPERGLUE_TARBALL}"
        if [[ -f "${dependency_tarball}" ]]; then
            log "Loading first-install dependency from ${dependency_tarball} ..."
            docker load -i "${dependency_tarball}"
            dependency_loaded=true
            break
        fi
    done
    if [[ "${dependency_loaded}" != "true" ]] \
            || ! docker image inspect "${SUPERGLUE_IMAGE}" >/dev/null 2>&1; then
        die "missing ${SUPERGLUE_IMAGE}; place ${SUPERGLUE_TARBALL} beside the dashboard archive and retry"
    fi
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
env_next=".env.next.$$"
if [[ -f .env ]]; then
    grep -v '^INSIGHT_VERSION=' .env > "${env_next}" || true
else
    : > "${env_next}"
fi
printf 'INSIGHT_VERSION=%s\n' "${version}" >> "${env_next}"
mv "${env_next}" .env

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

log "Synchronizing the host offline voice runtime from ${IMAGE_NAME}:${version} ..."
sync_host_voice_runtime "${IMAGE_NAME}:${version}"
"${VOICE_INSTALLER}" --if-ready

log "Done. Dashboard is running ${IMAGE_NAME}:${version} on port ${PORT}."
if [[ -n "${prev_version}" && "${prev_version}" != "${version}" ]]; then
    log "Previous version ${prev_version} is still loaded; roll back anytime with: ./update.sh --rollback ${prev_version}"
fi
