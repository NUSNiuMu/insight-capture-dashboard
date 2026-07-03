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

# The kiosk browser is vendored into the image at a fixed location (see
# Dockerfile's PLAYWRIGHT_BROWSERS_PATH) rather than a bundled revision
# folder name, so resolve the actual binary path at run time.
CHROME_BIN="$(find "${PLAYWRIGHT_BROWSERS_PATH:-/opt/pw-browsers}" -maxdepth 3 -type f -name chrome -path '*/chrome-linux/chrome' 2>/dev/null | head -1)"
if [[ -z "${CHROME_BIN}" ]]; then
  echo "Kiosk Chromium binary not found under ${PLAYWRIGHT_BROWSERS_PATH:-/opt/pw-browsers} -- rebuild the image (docker compose build)." >&2
  exit 1
fi

# --kiosk: true fullscreen, no window chrome, matches the previous PyQt5
# window's full-1920x1080 geometry without needing explicit --window-size
# (kiosk mode fills whatever display it's given). --no-sandbox is required
# because this runs as root in the container; the container boundary is the
# actual sandbox here, same trust model as the code it replaces.
exec "${CHROME_BIN}" \
  --kiosk \
  --no-sandbox \
  --no-first-run \
  --disable-session-crashed-bubble \
  --disable-infobars \
  "${URL}"
