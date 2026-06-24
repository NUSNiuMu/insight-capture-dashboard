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
  if [[ -n "${ROS_DOMAIN_ID+x}" && "${ROS_DOMAIN_ID}" != "${DEFAULT_DOMAIN_ID}" ]]; then
    echo "Ignoring inherited ROS_DOMAIN_ID=${ROS_DOMAIN_ID}; using config ros_domain_id=${DEFAULT_DOMAIN_ID}. Set INSIGHT_ROS_DOMAIN_ID to override." >&2
  fi
  export ROS_DOMAIN_ID="${DEFAULT_DOMAIN_ID}"
fi
export DISPLAY="${DISPLAY:-:0}"
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb}"

PYQT_PLUGIN_PATH="$(python3 - <<'PY'
from PyQt5.QtCore import QLibraryInfo
print(QLibraryInfo.location(QLibraryInfo.PluginsPath))
PY
)"
if [[ -n "${PYQT_PLUGIN_PATH}" ]]; then
  export QT_QPA_PLATFORM_PLUGIN_PATH="${PYQT_PLUGIN_PATH}"
fi

set +u
source /opt/ros/humble/setup.bash
set -u

export ROS_LOG_DIR="${ROS_LOG_DIR:-/tmp/ros_logs}"
mkdir -p "${ROS_LOG_DIR}"

bash "${SCRIPT_DIR}/start_topic_relay.sh"

echo "ROS_DOMAIN_ID: ${ROS_DOMAIN_ID}"
echo "DISPLAY: ${DISPLAY}"
echo "QT_QPA_PLATFORM_PLUGIN_PATH: ${QT_QPA_PLATFORM_PLUGIN_PATH:-unset}"
exec python3 "${CORE_SCRIPTS_DIR}/multi_camera_dashboard_qt.py" --config "${CONFIG_PATH}"
