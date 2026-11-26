#!/usr/bin/env bash
# Build one self-extracting offline Release for installation and upgrades.
# Usage: ./deploy/build_release.sh v1.2.0 [indexed-arm64-offline-debs]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
IMAGE_NAME="insight-dashboard"
SUPERGLUE_IMAGE="insight-superglue-validation:25.04"

version="${1:?usage: $0 <version, e.g. v1.2.0>}"
[[ "${version}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([.-].+)?$ ]] \
    || { echo "ERROR: version must look like v1.2.0 (got: ${version})" >&2; exit 1; }

offline_debs="${2:-}"
if [[ -n "$offline_debs" ]]; then
    offline_debs="$(realpath -e "$offline_debs")"
    [[ -f "$offline_debs/Packages" || -f "$offline_debs/Packages.gz" ]] || {
        echo "ERROR: missing APT package index in $offline_debs" >&2
        exit 1
    }
fi

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

# One payload carries every image and the current installer. No separate dependencies.
staging="$(mktemp -d "${ROOT_DIR}/release/.release.XXXXXX")"
trap 'rm -rf "${staging}"' EXIT
log "Saving the three runtime images ..."
docker save "${IMAGE_NAME}:${version}" "insight-loopervio:${version}" "${SUPERGLUE_IMAGE}" \
    > "${staging}/images.tar"
cp deploy/{update.sh,release_compose.py} "${staging}/"
payload_files=(update.sh release_compose.py images.tar)
if [[ -n "$offline_debs" ]]; then
    cp -a -- "$offline_debs" "$staging/offline-debs"
    payload_files+=(offline-debs)
fi
(cd "$staging" && find "${payload_files[@]}" -type f -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)

release_file="release/${IMAGE_NAME}-${version}.run"
log "Packaging ${release_file} ..."
cat deploy/upgrade_launcher.sh > "${staging}/release.run"
tar -C "${staging}" -cf - SHA256SUMS "${payload_files[@]}" | gzip -1 >> "${staging}/release.run"
chmod +x "${staging}/release.run"
mv "${staging}/release.run" "${release_file}"
(cd release && sha256sum "${IMAGE_NAME}-${version}.run") > "release/SHA256SUMS-${version}.txt"

log "Done: ${release_file} ($(du -h "${release_file}" | cut -f1))"
echo "Install or upgrade: bash ${release_file} /path/to/installation"
