#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
report_path="/tmp/insight_system_$(date +%Y%m%d_%H%M%S).json"
camera_identity_default="${HOME}/.ssh/insight_camera_ed25519"
repair_requested=false

for argument in "$@"; do
    if [[ "${argument}" == "--repair" ]]; then
        repair_requested=true
        break
    fi
done

if [[ "${repair_requested}" == "true" && -t 0 ]] && command -v sudo >/dev/null 2>&1; then
    if ! sudo -v; then
        printf '警告: 未取得 sudo 凭据；需要主机权限的修复项将失败并保留证据。\n' >&2
    fi
fi

if [[ -z "${INSIGHT_CAMERA_SSH_IDENTITY:-}" \
        && -z "${INSIGHT_CAMERA_SSH_PASSWORD:-}" \
        && -f "${camera_identity_default}" ]]; then
    INSIGHT_CAMERA_SSH_IDENTITY="${camera_identity_default}"
    export INSIGHT_CAMERA_SSH_IDENTITY
fi

if [[ -z "${INSIGHT_CAMERA_SSH_IDENTITY:-}" \
        && -z "${INSIGHT_CAMERA_SSH_PASSWORD:-}" && -t 0 ]]; then
    read -r -s -p "相机 SSH 密码（仅本次使用，不会保存）: " \
        INSIGHT_CAMERA_SSH_PASSWORD
    printf '\n'
    export INSIGHT_CAMERA_SSH_PASSWORD
fi

cd "${project_root}"
exec python3 scripts/system_doctor.py \
    --verbose \
    --output "${report_path}" \
    "$@"
