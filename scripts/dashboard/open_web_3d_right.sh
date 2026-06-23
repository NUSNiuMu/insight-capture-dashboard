#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CORE_SCRIPTS_DIR="${ROOT_DIR}/scripts"
CONFIG_PATH="${ROOT_DIR}/config/cameras.json"
if [[ -n "${WEB_PORT+x}" ]]; then
  WEB_PORT_EXPLICIT=1
else
  WEB_PORT=8765
  WEB_PORT_EXPLICIT=
fi
WEB_HOST="${WEB_HOST:-127.0.0.1}"
ROS_LOG_DIR="${ROS_LOG_DIR:-/tmp/ros_logs}"
export ROS_LOG_DIR
mkdir -p "${ROS_LOG_DIR}"
DEFAULT_DOMAIN_ID="$(python3 "${CORE_SCRIPTS_DIR}/camera_setup.py" --config "${CONFIG_PATH}" --ros-domain-id)"
USER_URL="${1:-}"

export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-${HOME}/.Xauthority}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"
export XDG_SESSION_TYPE="${XDG_SESSION_TYPE:-x11}"
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb}"
export QTWEBENGINE_DISABLE_SANDBOX="${QTWEBENGINE_DISABLE_SANDBOX:-1}"
unset WAYLAND_DISPLAY

PYQT_PLUGIN_PATH="$(python3 - <<'PY'
from PyQt5.QtCore import QLibraryInfo
print(QLibraryInfo.location(QLibraryInfo.PluginsPath))
PY
)"
if [[ -n "${PYQT_PLUGIN_PATH}" ]]; then
  export QT_QPA_PLATFORM_PLUGIN_PATH="${PYQT_PLUGIN_PATH}"
fi

ensure_web_backend() {
  health_check() {
    python3 - "${WEB_HOST}" "${WEB_PORT}" "${DEFAULT_DOMAIN_ID}" <<'PY'
import json
import sys
import urllib.request

host = sys.argv[1]
port = sys.argv[2]
expected_domain_id = sys.argv[3]
url = f"http://{host}:{port}/healthz"
try:
    with urllib.request.urlopen(url, timeout=0.7) as response:
        if response.status != 200:
            raise SystemExit(1)
        payload = json.loads(response.read().decode("utf-8"))
        actual_domain_id = str(payload.get("ros_domain_id", ""))
        raise SystemExit(0 if actual_domain_id == expected_domain_id else 1)
except Exception:
    raise SystemExit(1)
PY
  }

  if health_check
  then
    return 0
  fi

  if [[ -z "${WEB_PORT_EXPLICIT}" ]]; then
    local original_port="${WEB_PORT}"
    local candidate_port
    for candidate_port in $(seq "$((WEB_PORT + 1))" "$((WEB_PORT + 20))"); do
      WEB_PORT="${candidate_port}"
      if health_check; then
        echo "Using existing web dashboard backend on ${WEB_HOST}:${WEB_PORT} (ROS_DOMAIN_ID=${DEFAULT_DOMAIN_ID})"
        return 0
      fi
      if python3 - "${WEB_HOST}" "${WEB_PORT}" <<'PY'
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.settimeout(0.2)
    raise SystemExit(1 if sock.connect_ex((host, port)) == 0 else 0)
PY
      then
        echo "Port ${WEB_HOST}:${original_port} is not serving ROS_DOMAIN_ID=${DEFAULT_DOMAIN_ID}; using ${WEB_HOST}:${WEB_PORT}"
        break
      fi
    done
  fi

  export ROS_DOMAIN_ID="${DEFAULT_DOMAIN_ID}"

  set +u
  source /opt/ros/humble/setup.bash
  set -u

  local backend_log="${ROS_LOG_DIR}/web_dashboard_backend.log"
  echo "Starting web dashboard backend on ${WEB_HOST}:${WEB_PORT} (ROS_DOMAIN_ID=${ROS_DOMAIN_ID})"
  nohup python3 "${CORE_SCRIPTS_DIR}/multi_camera_dashboard_web.py" \
    --config "${CONFIG_PATH}" \
    --host "${WEB_HOST}" \
    --port "${WEB_PORT}" \
    >"${backend_log}" 2>&1 &

  local attempt
  for attempt in $(seq 1 50); do
    if health_check
    then
      return 0
    fi
    sleep 0.2
  done

  python3 - "${WEB_HOST}" "${WEB_PORT}" "${DEFAULT_DOMAIN_ID}" <<'PY' >&2 || true
import json
import sys
import urllib.request

host = sys.argv[1]
port = sys.argv[2]
expected_domain_id = sys.argv[3]
url = f"http://{host}:{port}/healthz"
try:
    with urllib.request.urlopen(url, timeout=0.7) as response:
        payload = json.loads(response.read().decode("utf-8"))
    actual_domain_id = str(payload.get("ros_domain_id", ""))
    if actual_domain_id != expected_domain_id:
        print(
            f"Port {host}:{port} is already serving ROS_DOMAIN_ID={actual_domain_id}; "
            f"expected {expected_domain_id}."
        )
except Exception:
    pass
PY
  echo "Web dashboard backend did not become ready. Check ${backend_log}" >&2
  return 1
}

ensure_web_backend

if [[ -n "${USER_URL}" ]]; then
  URL="${USER_URL}"
else
  URL="http://${WEB_HOST}:${WEB_PORT}/3d?v=$(date +%s)"
fi

exec python3 "${CORE_SCRIPTS_DIR}/web_3d_window.py" \
  --url "${URL}" \
  --config "${CONFIG_PATH}"
