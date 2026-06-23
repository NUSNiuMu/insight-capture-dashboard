#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

mkdir -p "${ROOT_DIR}/rosbags"
cd "${ROOT_DIR}"

MODE="${1:-app}"
if [[ "${MODE}" == "shell" || "${MODE}" == "bash" ]]; then
  shift
  COMMAND=(bash /workspace/insight_capture/scripts/docker/shell.sh)
else
  COMMAND=(bash /workspace/insight_capture/scripts/docker/start_app.sh)
fi

if docker compose version >/dev/null 2>&1; then
  if [[ "${MODE}" == "shell" || "${MODE}" == "bash" ]]; then
    docker compose run --rm insight_capture "${COMMAND[@]}" "$@"
  else
    docker compose up "$@"
  fi
elif command -v docker-compose >/dev/null 2>&1; then
  if [[ "${MODE}" == "shell" || "${MODE}" == "bash" ]]; then
    docker-compose run --rm insight_capture "${COMMAND[@]}" "$@"
  else
    docker-compose up "$@"
  fi
else
  tty_args=()
  if [[ -t 0 ]]; then
    tty_args=(-it)
  fi
  docker run --rm "${tty_args[@]}" \
    --name insight_capture_dashboard \
    --network host \
    --privileged \
    --ipc host \
    --pid host \
    -e "ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-20}" \
    -e "INSIGHT_ROSBAG_DIR=${INSIGHT_ROSBAG_DIR:-/workspace/rosbags}" \
    -e "BACKEND_HOST=${BACKEND_HOST:-0.0.0.0}" \
    -e "BACKEND_PORT=${BACKEND_PORT:-8765}" \
    -e "DISPLAY=${DISPLAY:-:0}" \
    -e "QT_X11_NO_MITSHM=1" \
    -e "QTWEBENGINE_DISABLE_SANDBOX=1" \
    -v "${ROOT_DIR}:/workspace/insight_capture" \
    -v "${ROOT_DIR}/rosbags:/workspace/rosbags" \
    -v "/dev:/dev" \
    -v "/tmp/.X11-unix:/tmp/.X11-unix:rw" \
    -v "/run/udev:/run/udev:ro" \
    insight-capture-dashboard:latest \
    "${COMMAND[@]}" "$@"
fi
