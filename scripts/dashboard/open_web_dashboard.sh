#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CORE_SCRIPTS_DIR="${ROOT_DIR}/scripts"
WEB_ROOT="${ROOT_DIR}/web_dashboard"
CONFIG_PATH="${ROOT_DIR}/config/cameras.json"
HOST_ARG_PRESENT=0
PORT_ARG_PRESENT=0

for arg in "$@"; do
  case "${arg}" in
    --host|--host=*)
      HOST_ARG_PRESENT=1
      ;;
    --port|--port=*)
      PORT_ARG_PRESENT=1
      ;;
  esac
done

if [[ -f /opt/ros/humble/setup.bash ]]; then
  set +u
  source /opt/ros/humble/setup.bash
  set -u
else
  echo "Missing /opt/ros/humble/setup.bash; cannot start web dashboard backend." >&2
  exit 1
fi

bash "${ROOT_DIR}/scripts/dev/check_env.sh" --fix --quiet
bash "${SCRIPT_DIR}/start_topic_relay.sh"

PYTHON_ARGS=(
  --config "${CONFIG_PATH}"
  --web-root "${WEB_ROOT}/generated"
)

if [[ "${HOST_ARG_PRESENT}" != "1" && -n "${INSIGHT_DASHBOARD_HOST:-}" ]]; then
  PYTHON_ARGS+=(--host "${INSIGHT_DASHBOARD_HOST}")
fi

if [[ "${PORT_ARG_PRESENT}" != "1" && -n "${INSIGHT_DASHBOARD_PORT:-}" ]]; then
  PYTHON_ARGS+=(--port "${INSIGHT_DASHBOARD_PORT}")
fi

exec python3 "${CORE_SCRIPTS_DIR}/multi_camera_dashboard_web.py" \
  "${PYTHON_ARGS[@]}" \
  "$@"
