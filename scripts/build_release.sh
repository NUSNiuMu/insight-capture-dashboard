#!/usr/bin/env bash
# Build a customer release: the docker image tarball (the thing customers get
# for every upgrade) plus the deploy bundle (one-time first-install package
# with the compose file, update.sh, run_dashboard.sh and README).
#
# Run on a Jetson (arm64) from anywhere inside the repo:
#   ./scripts/build_release.sh v1.2.0
#
# Produces:
#   release/insight-dashboard-v1.2.0.tar.gz          # image; every upgrade
#   release/insight-dashboard-deploy-v1.2.0.tar.gz   # bundle; first install only
#
# Delivery: first install = send both; upgrade = send just the image tarball,
# customer runs ./update.sh insight-dashboard-v1.2.0.tar.gz

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
IMAGE_NAME="insight-dashboard"

version="${1:?usage: $0 <version, e.g. v1.2.0>}"
[[ "${version}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([.-].+)?$ ]] \
    || { echo "ERROR: version must look like v1.2.0 (got: ${version})" >&2; exit 1; }

cd "${ROOT_DIR}"
mkdir -p release

log() { echo "[$(date '+%H:%M:%S')] $*"; }

log "Building ${IMAGE_NAME}:${version} ..."
# --network host: same Jetson iptables raw-table workaround as docker-compose.yml
docker build --network host -t "${IMAGE_NAME}:${version}" .

image_tarball="release/${IMAGE_NAME}-${version}.tar.gz"
log "Saving image to ${image_tarball} (several GB; takes a while)..."
docker save "${IMAGE_NAME}:${version}" | gzip > "${image_tarball}"

log "Assembling deploy bundle..."
bundle_dir="release/${IMAGE_NAME}-deploy-${version}"
rm -rf "${bundle_dir}"
mkdir -p "${bundle_dir}/scripts/systemd"
cp deploy/docker-compose.yml deploy/update.sh deploy/README.md "${bundle_dir}/"
# run_dashboard.sh resolves the compose project root as its parent dir, so it
# keeps working from <bundle>/scripts/ exactly like from the repo.
cp scripts/run_dashboard.sh "${bundle_dir}/scripts/"
chmod +x "${bundle_dir}/update.sh" "${bundle_dir}/scripts/run_dashboard.sh"

# Host-level setup (sysctl DDS receive buffers + boot-time camera-reboot
# unit) used to only happen via scripts/setup_host.sh, which needs the full
# source checkout -- the end-user path (this bundle) had no way to apply
# either, silently missing both the documented 10-24% frame-drop fix and the
# stale-DDS-participant boot race fix. Bundle host_setup.sh (thin wrapper
# around the same two steps setup_host.sh does) plus what it needs.
cp scripts/host_setup.sh "${bundle_dir}/scripts/"
cp scripts/reboot_cameras.sh "${bundle_dir}/scripts/"
cp scripts/systemd/insight-camera-reboot.service "${bundle_dir}/scripts/systemd/"
mkdir -p "${bundle_dir}/looper_cli"
cp -r looper_cli/looper_cli looper_cli/looper_cli.py "${bundle_dir}/looper_cli/"
find "${bundle_dir}/looper_cli" -name '__pycache__' -type d -exec rm -rf {} +
chmod +x "${bundle_dir}/scripts/host_setup.sh" "${bundle_dir}/scripts/reboot_cameras.sh"

bundle_tarball="release/${IMAGE_NAME}-deploy-${version}.tar.gz"
tar -C release -czf "${bundle_tarball}" "${IMAGE_NAME}-deploy-${version}"
rm -rf "${bundle_dir}"

log "Done."
echo
echo "  Image tarball  : ${image_tarball}  ($(du -h "${image_tarball}" | cut -f1))"
echo "  Deploy bundle  : ${bundle_tarball}  ($(du -h "${bundle_tarball}" | cut -f1))"
echo
echo "First install: send BOTH files; customer unpacks the bundle, then runs"
echo "  ./update.sh ${IMAGE_NAME}-${version}.tar.gz"
echo "Upgrade: send only the image tarball; customer runs the same command."
