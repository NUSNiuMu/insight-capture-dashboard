#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_PATH="${ROOT_DIR}/config/cameras.json"

DEFAULT_DOMAIN_ID="$(python3 "${SCRIPT_DIR}/camera_setup.py" --config "${CONFIG_PATH}" --ros-domain-id)"
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-${DEFAULT_DOMAIN_ID}}"
export DISPLAY="${DISPLAY:-:0}"

set +u
source /opt/ros/humble/setup.bash
set -u

export ROS_LOG_DIR="${ROS_LOG_DIR:-/tmp/ros_logs}"
mkdir -p "${ROS_LOG_DIR}"

echo "ROS_DOMAIN_ID: ${ROS_DOMAIN_ID}"
echo "DISPLAY: ${DISPLAY}"
exec python3 "${SCRIPT_DIR}/multi_camera_dashboard.py" --config "${CONFIG_PATH}"
