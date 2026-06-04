#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_PATH="${ROOT_DIR}/config/cameras.json"

DEFAULT_DOMAIN_ID="$(python3 "${SCRIPT_DIR}/camera_setup.py" --config "${CONFIG_PATH}" --ros-domain-id)"
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-${DEFAULT_DOMAIN_ID}}"

set +u
source /opt/ros/humble/setup.bash
set -u

export ROS_LOG_DIR="${ROS_LOG_DIR:-/tmp/ros_logs}"
mkdir -p "${ROS_LOG_DIR}"

OUTPUT_DIR="${1:-${ROOT_DIR}/bags/$(date +%Y%m%d_%H%M%S)}"
mapfile -t TOPICS < <(python3 "${SCRIPT_DIR}/camera_setup.py" --config "${CONFIG_PATH}" --record-topics)

mkdir -p "$(dirname "${OUTPUT_DIR}")"

echo "ROS_DOMAIN_ID: ${ROS_DOMAIN_ID}"
echo "Using topic list generated from: ${CONFIG_PATH}"
echo "Recording to: ${OUTPUT_DIR}"
echo "Topics:"
printf '  %s\n' "${TOPICS[@]}"

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  echo "DRY_RUN=1 set, not starting rosbag record."
  exit 0
fi

exec ros2 bag record -o "${OUTPUT_DIR}" "${TOPICS[@]}"
