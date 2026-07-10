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

# Firefox is the kiosk browser (baked into the Dockerfile at /opt/firefox --
# see its comment there for why: the vendored Playwright Chromium used
# previously has no H.264 decoder at all, so it could never show the WebRTC
# camera streams and was permanently stuck on the JPEG-polling fallback.
# Firefox bundles Cisco's OpenH264 plugin specifically for WebRTC).
FIREFOX_BIN="/opt/firefox/firefox"
if [[ ! -x "${FIREFOX_BIN}" ]]; then
  echo "Kiosk Firefox binary not found at ${FIREFOX_BIN} -- rebuild the image (docker compose build)." >&2
  exit 1
fi

# --kiosk: true fullscreen, no window chrome. --profile points at the
# baked-in profile (Dockerfile) that suppresses first-run dialogs, which
# would otherwise sit on top of the dashboard with no one at the keyboard
# to dismiss them. MOZ_DISABLE_CONTENT_SANDBOX is needed because this runs
# as root in the container; the container boundary is the actual sandbox
# here, same trust model as the Chromium kiosk this replaces.
FIREFOX_PROFILE="/opt/firefox-kiosk-profile"
KIOSK_LOG="${INSIGHT_KIOSK_LOG:-/tmp/insight-kiosk-firefox.log}"
export MOZ_DISABLE_CONTENT_SANDBOX=1
exec "${FIREFOX_BIN}" \
  --kiosk \
  --new-instance \
  --no-remote \
  --profile "${FIREFOX_PROFILE}" \
  "${URL}" \
  >"${KIOSK_LOG}" 2>&1
