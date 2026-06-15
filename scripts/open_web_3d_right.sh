#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
URL="${1:-http://127.0.0.1:8765/}"

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

exec python3 "${SCRIPT_DIR}/web_3d_window.py" \
  --url "${URL}" \
  --x 960 \
  --y 0 \
  --width 960 \
  --height 1080
