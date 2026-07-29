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
log "Saving images to ${image_tarball} (several GB; takes a while)..."
docker save "${IMAGE_NAME}:${version}" "${SUPERGLUE_IMAGE}" | gzip > "${image_tarball}"

log "Assembling deploy bundle..."
# Keep the installed directory stable; only artifacts and image tags vary.
bundle_dir="release/${IMAGE_NAME}-deploy"
rm -rf "${bundle_dir}"
mkdir -p "${bundle_dir}/scripts/systemd"
cp deploy/docker-compose.yml deploy/update.sh deploy/README.md "${bundle_dir}/"
# run_dashboard.sh resolves the compose project root as its parent dir, so it
# keeps working from <bundle>/scripts/ exactly like from the repo.
cp scripts/run_dashboard.sh "${bundle_dir}/scripts/"
chmod +x "${bundle_dir}/update.sh" "${bundle_dir}/scripts/run_dashboard.sh"

# Bundle the same host tuning used by source-checkout installations.
cp scripts/host_setup.sh "${bundle_dir}/scripts/"
cp scripts/reboot_cameras.sh "${bundle_dir}/scripts/"
cp scripts/systemd/insight-camera-reboot.service "${bundle_dir}/scripts/systemd/"
mkdir -p "${bundle_dir}/looper_cli"
cp -r looper_cli/looper_cli looper_cli/looper_cli.py "${bundle_dir}/looper_cli/"
find "${bundle_dir}/looper_cli" -name '__pycache__' -type d -exec rm -rf {} +
chmod +x "${bundle_dir}/scripts/host_setup.sh" "${bundle_dir}/scripts/reboot_cameras.sh"

bundle_tarball="release/${IMAGE_NAME}-deploy-${version}.tar.gz"
tar -C release -czf "${bundle_tarball}" "${IMAGE_NAME}-deploy"
rm -rf "${bundle_dir}"

log "Done."
echo
echo "  Image tarball  : ${image_tarball}  ($(du -h "${image_tarball}" | cut -f1))"
echo "  Deploy bundle  : ${bundle_tarball}  ($(du -h "${bundle_tarball}" | cut -f1))"
echo
echo "Image tarball now bundles ${IMAGE_NAME} and ${SUPERGLUE_IMAGE} (TensorRT/CUDA"
echo "runtime libs) -- noticeably bigger and slower to transfer than a single-image release."
echo
echo "First install: send BOTH files; customer unpacks the bundle, then runs"
echo "  ./update.sh ${IMAGE_NAME}-${version}.tar.gz"
echo "Upgrade: send only the image tarball; customer runs the same command."
