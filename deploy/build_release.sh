#!/usr/bin/env bash
# Build the customer image tarball and first-install deployment bundle.
# Usage: ./deploy/build_release.sh v1.2.0

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

# Both Jetson rigs ship the same code and all device templates.
selected_device="$(cat config/.device 2>/dev/null || echo "<none>")"
[[ "${selected_device}" == "jetson-nx" || "${selected_device}" == "jetson-nx-egohalo" ]] \
    || { echo "ERROR: select jetson-nx or jetson-nx-egohalo before building a Jetson release" >&2; exit 1; }

log "Building ${IMAGE_NAME}:${version} ..."
./native_dashboard/build.sh
# --network host: same Jetson iptables raw-table workaround as docker-compose.yml
# Backend images contain no desktop browser or Qt build tools.
docker build --network host --target runtime -t "${IMAGE_NAME}:${version}" .

log "Building ${SUPERGLUE_IMAGE} (mapping/relocalization dependency) ..."
docker build --network host -f Dockerfile.superglue-validation -t "${SUPERGLUE_IMAGE}" .

log "Building insight-loopervio:${version} ..."
docker build --network host -f native_mapping/Dockerfile.dependencies \
    -t insight-loopervio-deps:2.2 native_mapping
docker build --network host -t "insight-loopervio:${version}" native_mapping

image_tarball="release/${IMAGE_NAME}-${version}.tar.gz"
superglue_tarball="release/insight-superglue-validation-25.04.tar.gz"
log "Saving dashboard image to ${image_tarball} (several GB; takes a while)..."
docker save "${IMAGE_NAME}:${version}" "insight-loopervio:${version}" | gzip > "${image_tarball}"
log "Saving first-install dependency to ${superglue_tarball} ..."
docker save "${SUPERGLUE_IMAGE}" | gzip > "${superglue_tarball}"

log "Assembling deploy bundle..."
# Keep the installed directory stable; only artifacts and image tags vary.
bundle_dir="release/${IMAGE_NAME}-deploy"
rm -rf "${bundle_dir}"
mkdir -p \
    "${bundle_dir}/scripts" \
    "${bundle_dir}/deploy/systemd" \
    "${bundle_dir}/deploy/udev" \
    "${bundle_dir}/insight_capture" \
    "${bundle_dir}/tools/diagnostics"
mkdir -p "${bundle_dir}/native_dashboard" "${bundle_dir}/.native-build"
cp native_dashboard/run.sh "${bundle_dir}/native_dashboard/"
cp .native-build/insight-native "${bundle_dir}/.native-build/"
cp -a .native-build/runtime "${bundle_dir}/.native-build/"
cp deploy/docker-compose.yml deploy/update.sh deploy/README.md "${bundle_dir}/"
cp deploy/run_dashboard.sh \
    deploy/device_services.sh \
    deploy/migrate_release.py \
    deploy/host_setup.sh \
    deploy/configure_camera_network.sh \
    deploy/install_voice_control_service.sh \
    "${bundle_dir}/deploy/"
cp deploy/udev/99-insight-camera-rps.rules "${bundle_dir}/deploy/udev/"
cp scripts/run_dashboard.sh \
    scripts/run_voice.sh \
    scripts/select_device.sh \
    scripts/reboot_cameras.sh \
    scripts/sync_camera_restart.py \
    scripts/check_bag.py \
    "${bundle_dir}/scripts/"
cp insight_capture/__init__.py "${bundle_dir}/insight_capture/"
cp -r insight_capture/voice "${bundle_dir}/insight_capture/"
cp deploy/systemd/insight-voice-control.service.in "${bundle_dir}/deploy/systemd/"
cp deploy/systemd/insight-capture.service "${bundle_dir}/deploy/systemd/"
chmod +x "${bundle_dir}/update.sh" \
    "${bundle_dir}/deploy/run_dashboard.sh" \
    "${bundle_dir}/deploy/device_services.sh" \
    "${bundle_dir}/deploy/host_setup.sh" \
    "${bundle_dir}/deploy/configure_camera_network.sh" \
    "${bundle_dir}/deploy/install_voice_control_service.sh" \
    "${bundle_dir}/scripts/run_dashboard.sh" \
    "${bundle_dir}/scripts/run_voice.sh" \
    "${bundle_dir}/scripts/select_device.sh" \
    "${bundle_dir}/scripts/reboot_cameras.sh" \
    "${bundle_dir}/scripts/sync_camera_restart.py" \
    "${bundle_dir}/scripts/check_bag.py"

# Bundle the same host tuning used by source-checkout installations.
cp deploy/systemd/insight-camera-network.service "${bundle_dir}/deploy/systemd/"
cp deploy/systemd/insight-camera-reboot.service "${bundle_dir}/deploy/systemd/"
cp tools/diagnostics/sync_camera_restart.py "${bundle_dir}/tools/diagnostics/"
chmod +x "${bundle_dir}/tools/diagnostics/sync_camera_restart.py"

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
echo "For legacy Releases, also send the new deploy bundle and inference archive."
echo "Use the new update.sh --install-dir <existing-directory> <absolute-image-path>."
