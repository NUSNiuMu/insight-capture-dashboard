#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_URL="https://github.com/magicleap/SuperGluePretrainedNetwork.git"
PINNED_COMMIT="ddcf11f42e7e0732a0c4607648f9448ea8d73590"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
TARGET_DIR="${PROJECT_ROOT}/data/models/SuperGluePretrainedNetwork"

ACCEPTED=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --accept-license)
            ACCEPTED=true
            shift
            ;;
        --target)
            if [[ $# -lt 2 ]]; then
                echo "--target requires a path" >&2
                exit 2
            fi
            TARGET_DIR="$2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

if [[ "${ACCEPTED}" != true ]]; then
    echo "Refusing to download the research-only model without explicit acceptance."
    echo "Read: https://github.com/magicleap/SuperGluePretrainedNetwork/blob/master/LICENSE"
    echo "Usage: $0 --accept-license [--target PATH]"
    exit 2
fi

if [[ -e "${TARGET_DIR}" && ! -d "${TARGET_DIR}/.git" ]]; then
    echo "Target exists but is not a Git checkout: ${TARGET_DIR}" >&2
    exit 1
fi

mkdir -p "$(dirname "${TARGET_DIR}")"
if [[ ! -d "${TARGET_DIR}/.git" ]]; then
    git clone --filter=blob:none --no-checkout "${REPOSITORY_URL}" "${TARGET_DIR}"
fi

git -C "${TARGET_DIR}" fetch origin "${PINNED_COMMIT}"
git -C "${TARGET_DIR}" checkout --detach "${PINNED_COMMIT}"

ACTUAL_COMMIT="$(git -C "${TARGET_DIR}" rev-parse HEAD)"
if [[ "${ACTUAL_COMMIT}" != "${PINNED_COMMIT}" ]]; then
    echo "Unexpected SuperGlue commit: ${ACTUAL_COMMIT}" >&2
    exit 1
fi

for asset in \
    models/weights/superpoint_v1.pth \
    models/weights/superglue_indoor.pth \
    models/weights/superglue_outdoor.pth
do
    if [[ ! -s "${TARGET_DIR}/${asset}" ]]; then
        echo "Missing official asset: ${asset}" >&2
        exit 1
    fi
done

printf '%s  %s\n' \
    52b6708629640ca883673b5d5c097c4ddad37d8048b33f09c8ca0d69db12c40e \
    "${TARGET_DIR}/models/weights/superpoint_v1.pth" \
    0e710469be25ebe1e2ccf68edcae8b2945b0617c8e7e68412251d9d47f5052b1 \
    "${TARGET_DIR}/models/weights/superglue_indoor.pth" \
    2f5f5e9bb3febf07b69df633c4c3ff7a17f8af26a023aae2b9303d22339195bd \
    "${TARGET_DIR}/models/weights/superglue_outdoor.pth" \
    | sha256sum -c -

echo "Official SuperGlue validation assets ready at ${TARGET_DIR}"
echo "Pinned commit: ${ACTUAL_COMMIT}"
echo "These assets are research-only and must not enter a release image or Git commit."
