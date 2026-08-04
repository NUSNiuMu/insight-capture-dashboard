#!/usr/bin/env bash

set -euo pipefail

if [[ $# -ge 1 ]]; then
  URL="$1"
else
  URL="http://127.0.0.1:8765/?v=$(date +%s)"
fi

export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-${HOME}/.Xauthority}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"
export XDG_SESSION_TYPE="${XDG_SESSION_TYPE:-x11}"
unset WAYLAND_DISPLAY

# Firefox provides the H.264 support required by the WebRTC kiosk.
FIREFOX_BIN="/opt/firefox/firefox"
if [[ ! -x "${FIREFOX_BIN}" ]]; then
  echo "Kiosk Firefox binary not found at ${FIREFOX_BIN} -- rebuild the image (docker compose build)." >&2
  exit 1
fi

# The baked profile suppresses dialogs; an unprivileged user keeps sandboxing.
FIREFOX_PROFILE="/opt/firefox-kiosk-profile"
KIOSK_LOG="${INSIGHT_KIOSK_LOG:-/tmp/insight-kiosk-firefox.log}"

# The XDG_RUNTIME_DIR exported above resolved to root's (/run/user/0, or
# whatever root's uid was) -- kiosk can't use that. Give it its own.
KIOSK_RUNTIME_DIR="/tmp/kiosk-runtime"
mkdir -p "${KIOSK_RUNTIME_DIR}"
chown kiosk:kiosk "${KIOSK_RUNTIME_DIR}"
chmod 700 "${KIOSK_RUNTIME_DIR}"

exec su -s /bin/bash kiosk -c "
  export DISPLAY='${DISPLAY}'
  export XAUTHORITY='${XAUTHORITY}'
  export XDG_RUNTIME_DIR='${KIOSK_RUNTIME_DIR}'
  exec '${FIREFOX_BIN}' --kiosk --new-instance --no-remote --profile '${FIREFOX_PROFILE}' '${URL}'
" >"${KIOSK_LOG}" 2>&1
