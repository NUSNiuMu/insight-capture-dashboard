#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CORE_SCRIPTS_DIR="${ROOT_DIR}/scripts"
CONFIG_PATH="${ROOT_DIR}/config/cameras.json"

DEFAULT_DOMAIN_ID="$(python3 "${CORE_SCRIPTS_DIR}/camera_setup.py" --config "${CONFIG_PATH}" --ros-domain-id)"
if [[ -n "${INSIGHT_ROS_DOMAIN_ID+x}" ]]; then
  export ROS_DOMAIN_ID="${INSIGHT_ROS_DOMAIN_ID}"
else
  export ROS_DOMAIN_ID="${DEFAULT_DOMAIN_ID}"
fi

set +u
source /opt/ros/humble/setup.bash
set -u

export ROS_LOG_DIR="${ROS_LOG_DIR:-/tmp/ros_logs}"
mkdir -p "${ROS_LOG_DIR}"

PID_FILE="${INSIGHT_TOPIC_RELAY_PID_FILE:-/tmp/insight_topic_relay_${ROS_DOMAIN_ID}.pid}"
LOG_FILE="${INSIGHT_TOPIC_RELAY_LOG:-/tmp/insight_topic_relay_${ROS_DOMAIN_ID}.log}"

if [[ -f "${PID_FILE}" ]]; then
  existing_pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
  if [[ -n "${existing_pid}" ]] && kill -0 "${existing_pid}" 2>/dev/null; then
    if [[ "${CORE_SCRIPTS_DIR}/topic_relay.py" -nt "${PID_FILE}" || "${CONFIG_PATH}" -nt "${PID_FILE}" ]]; then
      echo "Restarting stale Insight topic relay: pid=${existing_pid}"
      kill "${existing_pid}" 2>/dev/null || true
      sleep 0.5
    else
      echo "Insight topic relay already running: pid=${existing_pid}"
      exit 0
    fi
  fi
fi

nohup python3 "${CORE_SCRIPTS_DIR}/topic_relay.py" --config "${CONFIG_PATH}" >"${LOG_FILE}" 2>&1 &
relay_pid="$!"
echo "${relay_pid}" >"${PID_FILE}"
echo "Started Insight topic relay: pid=${relay_pid} log=${LOG_FILE} ROS_DOMAIN_ID=${ROS_DOMAIN_ID}"
