#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_DIR="${SUPERGLUE_CHECKOUT:-${PROJECT_ROOT}/data/models/SuperGluePretrainedNetwork}"
BACKEND="${SUPERGLUE_BACKEND:-ipc}"

if [[ "${BACKEND}" == "official-torch" && ! -s "${MODEL_DIR}/models/weights/superpoint_v1.pth" ]]; then
    echo "Official model assets are missing." >&2
    echo "Run: scripts/setup_superglue_validation.sh --accept-license" >&2
    exit 2
fi

exec python3 -u "${PROJECT_ROOT}/scripts/insight9_sparse_mapper.py" \
    --backend "${BACKEND}" \
    --superglue-checkout "${MODEL_DIR}" \
    "$@"
