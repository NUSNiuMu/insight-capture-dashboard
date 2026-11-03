#!/usr/bin/env bash
# Install or upgrade a customer deployment.
#
# Usage:
#   ./update.sh --install-dir /path/to/installation /tmp/images.tar
#
# Refuses to restart while a recording is in progress (override: --force).
# Superseded Release images and packages are removed after successful startup.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${DASHBOARD_PORT:-8765}"
IMAGE_NAME="insight-dashboard"
SUPERGLUE_IMAGE="insight-superglue-validation:25.04"


log() { echo "[$(date '+%H:%M:%S')] $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

sync_host_runtime() {
    local image="$1"
    local container temporary
    temporary="$(mktemp -d "${SCRIPT_DIR}/.update.XXXXXX")"
    container="$(docker create "${image}")"
    cleanup_voice_sync() {
        docker rm "${container}" >/dev/null 2>&1 || true
        rm -rf "${temporary}"
    }
    trap cleanup_voice_sync EXIT

    docker cp "${container}:/workspaces/insight_capture/insight_capture/voice" "${temporary}/voice"
    docker cp "${container}:/workspaces/insight_capture/insight_capture/__init__.py" "${temporary}/insight_capture_init.py"
    docker cp "${container}:/workspaces/insight_capture/deploy" "${temporary}/deploy"
    docker cp "${container}:/workspaces/insight_capture/scripts" "${temporary}/scripts"
    docker cp "${container}:/workspaces/insight_capture/tools/diagnostics/sync_camera_restart.py" "${temporary}/sync_camera_restart.py"
    docker cp "${container}:/workspaces/insight_capture/config/devices" "${temporary}/devices"
    docker cp "${container}:/opt/insight/native" "${temporary}/native"
    docker cp "${container}:/workspaces/insight_capture/native_dashboard/run.sh" "${temporary}/run-native.sh"

    mkdir -p "${HOST_RUNTIME_ROOT}/insight_capture" "${HOST_RUNTIME_ROOT}/scripts" \
        "${HOST_RUNTIME_ROOT}/deploy" "${HOST_RUNTIME_ROOT}/tools/diagnostics" \
        "${SCRIPT_DIR}/config" "${SCRIPT_DIR}/native_dashboard" "${SCRIPT_DIR}/.native-build"
    rm -rf "${HOST_RUNTIME_ROOT}/insight_capture/voice"
    mv "${temporary}/voice" "${HOST_RUNTIME_ROOT}/insight_capture/voice"
    install -m 0644 "${temporary}/insight_capture_init.py" "${HOST_RUNTIME_ROOT}/insight_capture/__init__.py"
    cp -a "${temporary}/deploy/." "${HOST_RUNTIME_ROOT}/deploy/"
    cp -a "${temporary}/scripts/." "${HOST_RUNTIME_ROOT}/scripts/"
    install -m 0755 "${temporary}/sync_camera_restart.py" "${HOST_RUNTIME_ROOT}/tools/diagnostics/sync_camera_restart.py"
    install -m 0755 "${temporary}/deploy/update.sh" "${SCRIPT_DIR}/.update.sh.next"
    mv "${SCRIPT_DIR}/.update.sh.next" "${SCRIPT_DIR}/update.sh"
    install -m 0644 "${temporary}/deploy/docker-compose.yml" "${SCRIPT_DIR}/docker-compose.yml"
    cp -r "${temporary}/devices" "${SCRIPT_DIR}/config/"
    mkdir -p "${temporary}/qt-runtime"
    tar -xf "${temporary}/native/qt-runtime.tar" -C "${temporary}/qt-runtime"
    install -m 0755 "${temporary}/run-native.sh" "${SCRIPT_DIR}/native_dashboard/run.sh"
    if [[ -d "${SCRIPT_DIR}/.native-build/runtime" ]]; then
        mv "${SCRIPT_DIR}/.native-build/runtime" "${temporary}/old-qt-runtime"
    fi
    mv "${temporary}/qt-runtime" "${SCRIPT_DIR}/.native-build/runtime"
    install -m 0755 "${temporary}/native/insight-native" "${SCRIPT_DIR}/.native-build/.insight-native.next"
    mv "${SCRIPT_DIR}/.native-build/.insight-native.next" "${SCRIPT_DIR}/.native-build/insight-native"

    trap - EXIT
    cleanup_voice_sync
}

force=false
tarball=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --force) force=true; shift ;;
        --install-dir) SCRIPT_DIR="$(cd "${2:?--install-dir needs the existing Release directory}" && pwd)"; shift 2 ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) die "unknown option: $1 (see --help)" ;;
        *) tarball="$(realpath -m "$1")"; shift ;;
    esac
done

cd "${SCRIPT_DIR}"
HOST_RUNTIME_ROOT="${SCRIPT_DIR}"
VOICE_INSTALLER="${SCRIPT_DIR}/deploy/install_voice_control_service.sh"
command -v docker >/dev/null || die "docker is not installed"
docker compose version >/dev/null 2>&1 || die "docker compose (v2 plugin) is not available"

# ── Don't kill an in-flight recording ────────────────────────────────────────
if curl -sf "http://localhost:${PORT}/api/recording/status" 2>/dev/null \
        | python3 -c 'import json,sys; s=json.load(sys.stdin); sys.exit(0 if s.get("recording") or s.get("state") in {"starting", "saving"} or s.get("merge_state") in {"saving", "merging"} else 1)' 2>/dev/null; then
    if [[ "${force}" == "true" ]]; then
        log "WARNING: a recording is in progress; --force given, proceeding anyway (the recording will be cut short)."
    else
        die "a recording is in progress -- finish it first, or re-run with --force to interrupt it"
    fi
fi

# Keep the previous dependency ID before docker load replaces its stable tag.
previous_inference="$(docker image inspect --format '{{.Id}}' "${SUPERGLUE_IMAGE}" 2>/dev/null || true)"
[[ -n "${tarball}" ]] || die "usage: $0 <image-archive> (see --help)"
[[ -f "${tarball}" ]] || die "file not found: ${tarball}"
log "Loading image from ${tarball} (this can take a few minutes)..."
load_output="$(docker load -i "${tarball}")"
echo "${load_output}"
version="$(sed -n "s/^Loaded image: ${IMAGE_NAME}://p" <<<"${load_output}" | head -1)"
[[ -n "${version}" ]] || die "archive did not contain an ${IMAGE_NAME}:* image"

# The single Release payload contains the inference and native-core images too.
docker image inspect "${SUPERGLUE_IMAGE}" >/dev/null 2>&1 \
    || die "missing ${SUPERGLUE_IMAGE}; use the complete Release installer"

docker image inspect "insight-loopervio:${version}" >/dev/null 2>&1 \
    || die "missing insight-loopervio:${version}; load the matching release archive"

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

# ── Switch the installed version ───────────────────────────────────────────
env_next=".env.next.$$"
if [[ -f .env ]]; then
    grep -v '^INSIGHT_VERSION=' .env > "${env_next}" || true
else
    : > "${env_next}"
fi
printf 'INSIGHT_VERSION=%s\n' "${version}" >> "${env_next}"
mv "${env_next}" .env

log "Starting ${IMAGE_NAME}:${version} ..."
log "Synchronizing host tools and Compose definitions from ${IMAGE_NAME}:${version} ..."
# Stop the previous Compose project without removing volumes or recordings.
if [[ -f docker-compose.yml ]]; then
    docker compose --profile "*" down --remove-orphans || log "WARNING: old Compose teardown was incomplete; continuing with the new definitions."
fi
sync_host_runtime "${IMAGE_NAME}:${version}"
log "Migrating installed configuration; calibration and original recordings are retained ..."
docker run --rm --network none --entrypoint python3 \
    -v "${SCRIPT_DIR}:/installation" "${IMAGE_NAME}:${version}" \
    /workspaces/insight_capture/deploy/migrate_release.py /installation
ROOT_DIR="${SCRIPT_DIR}"
source "${SCRIPT_DIR}/deploy/device_services.sh"
# Keep the phone/API reachable while inference engines initialize.
load_device_services
prepare_recording_bind_source
docker compose up -d --no-deps insight-dashboard
if ! start_device_services; then
    log "WARNING: one or more localization services are still unavailable; check docker compose logs. The recording API remains available."
fi

log "Waiting for the backend to become healthy on :${PORT} ..."
deadline=$(( $(date +%s) + 90 ))
until curl -sf "http://localhost:${PORT}/healthz" >/dev/null 2>&1; do
    if (( $(date +%s) > deadline )); then
        echo "ERROR: backend did not come up within 90s. Check: docker compose logs -f" >&2
        echo "Old Release artifacts have been retained." >&2
        exit 1
    fi
    sleep 2
done

if [[ -x "${VOICE_INSTALLER}" ]]; then
    "${VOICE_INSTALLER}" --if-ready || log "WARNING: optional voice service was not updated; other services remain installed."
fi

log "Removing superseded Release images and installation packages ..."
python3 "${SCRIPT_DIR}/deploy/cleanup_release.py" "${SCRIPT_DIR}" "${version}" \
    --installer "${INSIGHT_RELEASE_INSTALLER:-${tarball}}" \
    --previous-inference "${previous_inference}" --services "${device_services[@]}" \
    || log "WARNING: cleanup was incomplete; the new services remain installed."

log "Done. Dashboard is running ${IMAGE_NAME}:${version} on port ${PORT}."
log "On the Jetson desktop, launch ./native_dashboard/run.sh."
