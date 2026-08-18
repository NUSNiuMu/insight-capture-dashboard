#!/usr/bin/env bash
# Build the customer image tarball and first-install deployment bundle.
# Usage: ./scripts/build_release.sh v1.2.0

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
IMAGE_NAME="insight-dashboard"
SUPERGLUE_IMAGE="insight-superglue-validation:25.04"

version="${1:?usage: $0 <version, e.g. v1.2.0>}"
[[ "${version}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([.-].+)?$ ]] \
    || { echo "ERROR: version must look like v1.2.0 (got: ${version})" >&2; exit 1; }

cd "${ROOT_DIR}"
mkdir -p release

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# Customer releases must contain the jetson-nx live profile.
selected_device="$(cat config/.device 2>/dev/null || echo "<none>")"
[[ "${selected_device}" == "jetson-nx" ]] \
    || { echo "ERROR: config/ is currently set to '${selected_device}', not 'jetson-nx' -- run ./scripts/select_device.sh jetson-nx first" >&2; exit 1; }

log "Building ${IMAGE_NAME}:${version} ..."
# --network host: same Jetson iptables raw-table workaround as docker-compose.yml
# --target runtime: skips the dev-only headless Chromium stage (see Dockerfile)
docker build --network host --target runtime -t "${IMAGE_NAME}:${version}" .

log "Building ${SUPERGLUE_IMAGE} (mapping/relocalization dependency) ..."
docker build --network host -f Dockerfile.superglue-validation -t "${SUPERGLUE_IMAGE}" .

image_tarball="release/${IMAGE_NAME}-${version}.tar.gz"
superglue_tarball="release/insight-superglue-validation-25.04.tar.gz"
log "Saving dashboard image to ${image_tarball} (several GB; takes a while)..."
docker save "${IMAGE_NAME}:${version}" | gzip > "${image_tarball}"
log "Saving first-install dependency to ${superglue_tarball} ..."
docker save "${SUPERGLUE_IMAGE}" | gzip > "${superglue_tarball}"

log "Assembling deploy bundle..."
# Keep the installed directory stable; only artifacts and image tags vary.
bundle_dir="release/${IMAGE_NAME}-deploy"
rm -rf "${bundle_dir}"
mkdir -p "${bundle_dir}/scripts" "${bundle_dir}/deploy/systemd" "${bundle_dir}/insight_capture" "${bundle_dir}/tools"
cp deploy/docker-compose.yml deploy/update.sh deploy/README.md "${bundle_dir}/"
# run_dashboard.sh resolves the compose project root as its parent dir, so it
# keeps working from <bundle>/scripts/ exactly like from the repo.
cp scripts/run_dashboard.sh "${bundle_dir}/scripts/"
cp scripts/openclaw_voice_bridge.py "${bundle_dir}/scripts/"
cp scripts/_bootstrap.py "${bundle_dir}/scripts/"
cp insight_capture/__init__.py "${bundle_dir}/insight_capture/"
cp -r insight_capture/voice "${bundle_dir}/insight_capture/"
cp scripts/run_voice_control.sh "${bundle_dir}/scripts/"
cp scripts/run_voice.sh "${bundle_dir}/scripts/"
cp scripts/run_openclaw_voice.sh "${bundle_dir}/scripts/"
cp scripts/install_voice_control_service.sh "${bundle_dir}/scripts/"
cp scripts/install_openclaw_voice_service.sh "${bundle_dir}/scripts/"
cp deploy/systemd/insight-voice-control.service.in "${bundle_dir}/deploy/systemd/"
cp deploy/systemd/openclaw-voice.service.in "${bundle_dir}/deploy/systemd/"
cp deploy/systemd/insight-capture.service "${bundle_dir}/deploy/systemd/"
chmod +x "${bundle_dir}/update.sh" \
    "${bundle_dir}/scripts/run_dashboard.sh" \
    "${bundle_dir}/scripts/run_voice_control.sh" \
    "${bundle_dir}/scripts/run_voice.sh" \
    "${bundle_dir}/scripts/run_openclaw_voice.sh" \
    "${bundle_dir}/scripts/install_voice_control_service.sh" \
    "${bundle_dir}/scripts/install_openclaw_voice_service.sh"

# Bundle the same host tuning used by source-checkout installations.
cp scripts/host_setup.sh "${bundle_dir}/scripts/"
cp scripts/configure_camera_network.sh "${bundle_dir}/scripts/"
cp scripts/reboot_cameras.sh "${bundle_dir}/scripts/"
cp scripts/sync_camera_restart.py "${bundle_dir}/scripts/"
cp scripts/README.md "${bundle_dir}/scripts/"
cp deploy/systemd/insight-camera-network.service "${bundle_dir}/deploy/systemd/"
mkdir -p "${bundle_dir}/scripts/udev"
cp scripts/udev/99-insight-camera-rps.rules "${bundle_dir}/scripts/udev/"
cp deploy/systemd/insight-camera-reboot.service "${bundle_dir}/deploy/systemd/"
cp -r tools/device_cli "${bundle_dir}/tools/"
find "${bundle_dir}/tools/device_cli" -name '__pycache__' -type d -exec rm -rf {} +
chmod +x "${bundle_dir}/scripts/host_setup.sh" \
    "${bundle_dir}/scripts/configure_camera_network.sh" \
    "${bundle_dir}/scripts/reboot_cameras.sh" \
    "${bundle_dir}/scripts/sync_camera_restart.py"

bundle_tarball="release/${IMAGE_NAME}-deploy-${version}.tar.gz"
tar -C release -czf "${bundle_tarball}" "${IMAGE_NAME}-deploy"
rm -rf "${bundle_dir}"

log "Done."
echo
echo "  Image tarball  : ${image_tarball}  ($(du -h "${image_tarball}" | cut -f1))"
echo "  Dependency     : ${superglue_tarball}  ($(du -h "${superglue_tarball}" | cut -f1))"
echo "  Deploy bundle  : ${bundle_tarball}  ($(du -h "${bundle_tarball}" | cut -f1))"
echo
echo "First install: send all three files. Keep the two image archives together;"
echo "update.sh auto-loads ${SUPERGLUE_IMAGE} when it is not installed, then runs"
echo "  ./update.sh ${IMAGE_NAME}-${version}.tar.gz"
echo "Upgrade: send only ${IMAGE_NAME}-${version}.tar.gz; the stable SuperGlue"
echo "dependency already on the device is reused."
