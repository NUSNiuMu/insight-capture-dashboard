#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

export DISPLAY="${DISPLAY:-:0}"
if [[ -z "${XAUTHORITY:-}" ]]; then
    SESSION_UID="$(id -u)"
    export XAUTHORITY="/run/user/${SESSION_UID}/gdm/Xauthority"
fi

if [[ ! -S /tmp/.X11-unix/X0 ]]; then
    echo "X11 display socket /tmp/.X11-unix/X0 is unavailable." >&2
    exit 1
fi
if [[ ! -r "${XAUTHORITY}" ]]; then
    echo "XAUTHORITY is not readable: ${XAUTHORITY}" >&2
    exit 1
fi

XHOST_GRANTED=0
VALIDATION_STARTED=0
cleanup() {
    local exit_status=$?
    trap - EXIT INT TERM
    if [[ "${VALIDATION_STARTED}" -eq 1 ]]; then
        echo "Stopping sparse localization validation services..."
        docker compose --profile mapping-validation stop \
            insight3-global-localizer \
            insight9-sparse-mapper \
            superglue-inference || true
    fi
    if [[ "${XHOST_GRANTED}" -eq 1 ]]; then
        xhost -si:localuser:root >/dev/null 2>&1 || true
    fi
    exit "${exit_status}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

xhost +si:localuser:root >/dev/null
XHOST_GRANTED=1

echo "Starting a new sparse localization session; previous maps and paths are discarded."
VALIDATION_STARTED=1
docker compose --profile mapping-validation up -d --wait --wait-timeout 900 \
    superglue-inference
docker compose --profile mapping-validation stop insight9-dense-mapper
docker compose --profile mapping-validation up -d --no-deps --force-recreate \
    insight9-sparse-mapper \
    insight3-global-localizer

docker compose --profile mapping-validation run --rm --no-deps \
    insight9-mapping-rviz
