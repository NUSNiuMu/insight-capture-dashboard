#!/usr/bin/env bash

set -euo pipefail

if [[ $# -ge 1 ]]; then
  DASHBOARD_URL="$1"
else
  DASHBOARD_URL="http://127.0.0.1:8765/?v=$(date +%s)"
fi

export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-${HOME}/.Xauthority}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"
export XDG_SESSION_TYPE="${XDG_SESSION_TYPE:-x11}"
unset WAYLAND_DISPLAY

FIREFOX_BIN="/opt/firefox/firefox"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFS_SOURCE="${SCRIPT_DIR}/firefox-kiosk-user.js"
USER_CHROME_SOURCE="${SCRIPT_DIR}/firefox-kiosk-userChrome.css"
KIOSK_LOG="${INSIGHT_KIOSK_LOG:-/tmp/insight-kiosk-firefox.log}"
CAMERA_LOG="${INSIGHT_KIOSK_CAMERA_LOG:-/tmp/insight-kiosk-camera-firefox.log}"
KIOSK_RUNTIME_DIR="/tmp/kiosk-runtime"
KIOSK_LOCK="/tmp/insight-split-kiosk.lock"
SPLIT_KIOSK="${INSIGHT_KIOSK_SPLIT:-0}"
MAIN_WINDOW_MARKER="[insight-kiosk-scene]"
CAMERA_WINDOW_MARKER="[insight-kiosk-cameras"
CAMERA_FULLSCREEN_MARKER="[insight-kiosk-cameras-fullscreen]"

if [[ ! -x "${FIREFOX_BIN}" ]]; then
  echo "Kiosk Firefox binary not found at ${FIREFOX_BIN} -- rebuild the image (docker compose build)." >&2
  exit 1
fi

exec 9>"${KIOSK_LOCK}"
if ! flock -n 9; then
  echo "The on-device kiosk is already running." >&2
  exit 0
fi

mkdir -p "${KIOSK_RUNTIME_DIR}"
chown kiosk:kiosk "${KIOSK_RUNTIME_DIR}"
chmod 700 "${KIOSK_RUNTIME_DIR}"

KIOSK_SESSION_DIR="$(mktemp -d /tmp/insight-split-kiosk.XXXXXX)"
MAIN_PROFILE="${KIOSK_SESSION_DIR}/main-profile"
CAMERA_PROFILE="${KIOSK_SESSION_DIR}/camera-profile"
main_pid=""
camera_pid=""

chown kiosk:kiosk "${KIOSK_SESSION_DIR}"
chmod 700 "${KIOSK_SESSION_DIR}"

prepare_profile() {
  local profile_dir="$1"
  install -d -o kiosk -g kiosk "${profile_dir}"
  install -d -o kiosk -g kiosk "${profile_dir}/chrome"
  install -o kiosk -g kiosk -m 0644 "${PREFS_SOURCE}" "${profile_dir}/user.js"
  install -o kiosk -g kiosk -m 0644 "${USER_CHROME_SOURCE}" "${profile_dir}/chrome/userChrome.css"
}

prepare_profile "${MAIN_PROFILE}"
prepare_profile "${CAMERA_PROFILE}"

cleanup() {
  trap - EXIT INT TERM
  pkill -INT -u kiosk -f "${KIOSK_SESSION_DIR}" 2>/dev/null || true
  local shutdown_attempt
  for ((shutdown_attempt = 0; shutdown_attempt < 20; shutdown_attempt++)); do
    if ! pgrep -u kiosk -f "${KIOSK_SESSION_DIR}" >/dev/null 2>&1; then break; fi
    sleep 0.1
  done
  pkill -KILL -u kiosk -f "${KIOSK_SESSION_DIR}" 2>/dev/null || true
  pkill -KILL -f "${MAIN_PROFILE}" 2>/dev/null || true
  pkill -KILL -f "${CAMERA_PROFILE}" 2>/dev/null || true
  if [[ -n "${main_pid}" ]]; then kill -KILL "${main_pid}" 2>/dev/null || true; fi
  if [[ -n "${camera_pid}" ]]; then kill -KILL "${camera_pid}" 2>/dev/null || true; fi
  if [[ "${KIOSK_SESSION_DIR}" == /tmp/insight-split-kiosk.* ]]; then
    rm -rf "${KIOSK_SESSION_DIR}"
  fi
}
trap cleanup EXIT
trap 'cleanup; exit 0' INT TERM

launch_firefox() {
  local profile_dir="$1"
  local url="$2"
  local log_path="$3"
  local mode="$4"
  local launch_command
  if [[ "${mode}" == "kiosk" ]]; then
    printf -v launch_command \
      'export DISPLAY=%q XAUTHORITY=%q XDG_RUNTIME_DIR=%q; exec %q --kiosk --new-instance --no-remote --profile %q %q' \
      "${DISPLAY}" "${XAUTHORITY}" "${KIOSK_RUNTIME_DIR}" "${FIREFOX_BIN}" "${profile_dir}" "${url}"
  else
    printf -v launch_command \
      'export DISPLAY=%q XAUTHORITY=%q XDG_RUNTIME_DIR=%q; exec %q --new-instance --no-remote --profile %q %q' \
      "${DISPLAY}" "${XAUTHORITY}" "${KIOSK_RUNTIME_DIR}" "${FIREFOX_BIN}" "${profile_dir}" "${url}"
  fi
  su -s /bin/bash kiosk -c "${launch_command}" 9>&- >>"${log_path}" 2>&1 &
}

launch_main() {
  launch_firefox "${MAIN_PROFILE}" "${SCENE_URL}" "${KIOSK_LOG}" kiosk
  main_pid=$!
}

launch_cameras() {
  launch_firefox "${CAMERA_PROFILE}" "${CAMERA_URL}" "${CAMERA_LOG}" window
  camera_pid=$!
}

append_query() {
  local url="$1"
  local query="$2"
  if [[ "${url}" == *\?* ]]; then
    printf '%s&%s' "${url}" "${query}"
  else
    printf '%s?%s' "${url}" "${query}"
  fi
}

if [[ "${DASHBOARD_URL}" =~ ^(https?://[^/]+) ]]; then
  DASHBOARD_ORIGIN="${BASH_REMATCH[1]}"
else
  echo "Kiosk URL must be an absolute http(s) URL: ${DASHBOARD_URL}" >&2
  exit 1
fi

SCENE_URL="$(append_query "${DASHBOARD_URL}" "kiosk-role=scene")"
CAMERA_URL="$(append_query "${DASHBOARD_ORIGIN}/camera-wall" "kiosk-role=cameras")"

run_single_window() {
  SCENE_URL="${DASHBOARD_URL}"
  launch_main
  wait "${main_pid}"
}

if [[ "${SPLIT_KIOSK}" == "0" ]]; then
  run_single_window
  exit 0
fi

if ! command -v wmctrl >/dev/null 2>&1 \
  || ! command -v xprop >/dev/null 2>&1 \
  || ! command -v xdotool >/dev/null 2>&1; then
  echo "X11 window controls are unavailable; falling back to the single-window kiosk." >&2
  run_single_window
  exit 0
fi

desktop_line="$(wmctrl -d 2>/dev/null | awk '$2 == "*" { print; exit }' || true)"
if [[ "${desktop_line}" =~ DG:[[:space:]]*([0-9]+)x([0-9]+) ]]; then
  screen_width="${BASH_REMATCH[1]}"
  screen_height="${BASH_REMATCH[2]}"
else
  echo "Unable to read the X11 desktop geometry; falling back to the single-window kiosk." >&2
  run_single_window
  exit 0
fi

if (( screen_width < 1000 )); then
  echo "The display is too narrow for the split kiosk; using one window." >&2
  run_single_window
  exit 0
fi

rail_width="${INSIGHT_KIOSK_RAIL_WIDTH:-74}"
# Firefox's GTK window advertises a 500px minimum width even after its browser
# chrome is hidden. Match the scene placeholder to that native constraint.
camera_width="${INSIGHT_KIOSK_CAMERA_WIDTH:-500}"
if ! [[ "${rail_width}" =~ ^[0-9]+$ && "${camera_width}" =~ ^[0-9]+$ ]]; then
  echo "Kiosk rail and camera widths must be integers." >&2
  exit 1
fi
if (( camera_width < 500 )); then camera_width=500; fi
if (( camera_width > 800 )); then camera_width=800; fi
SCENE_URL="$(append_query "${SCENE_URL}" "kiosk-camera-width=${camera_width}")"

find_window() {
  local marker="$1"
  wmctrl -lp | awk -v marker="${marker}" 'index($0, marker) { print $1; exit }'
}

window_line() {
  local window_id="$1"
  wmctrl -l | awk -v window_id="${window_id}" '$1 == window_id { print; exit }'
}

show_camera_split() {
  local window_id="$1"
  xdotool windowmap --sync "${window_id}"
  xprop -id "${window_id}" -f _MOTIF_WM_HINTS 32c \
    -set _MOTIF_WM_HINTS "2, 0, 0, 0, 0"
  wmctrl -i -r "${window_id}" -b remove,fullscreen,maximized_vert,maximized_horz,hidden
  wmctrl -i -r "${window_id}" -e "0,${rail_width},0,${camera_width},${screen_height}"
  wmctrl -i -r "${window_id}" -b add,above,skip_taskbar,skip_pager
}

show_camera_fullscreen() {
  local window_id="$1"
  xdotool windowmap --sync "${window_id}"
  wmctrl -i -r "${window_id}" -b remove,hidden
  wmctrl -i -r "${window_id}" -b add,fullscreen,above,skip_taskbar,skip_pager
}

hide_camera_window() {
  local window_id="$1"
  wmctrl -i -r "${window_id}" -b remove,fullscreen,above
  xdotool windowunmap "${window_id}"
}

launch_main
launch_cameras

main_window_id=""
camera_window_id=""
camera_window_state=""

while true; do
  if ! kill -0 "${main_pid}" 2>/dev/null; then
    wait "${main_pid}" 2>/dev/null || true
    launch_main
    main_window_id=""
    camera_window_state=""
  fi
  if ! kill -0 "${camera_pid}" 2>/dev/null; then
    wait "${camera_pid}" 2>/dev/null || true
    launch_cameras
    camera_window_id=""
    camera_window_state=""
  fi

  if [[ -z "${main_window_id}" ]]; then
    main_window_id="$(find_window "${MAIN_WINDOW_MARKER}")"
  fi
  if [[ -z "${camera_window_id}" ]]; then
    camera_window_id="$(find_window "${CAMERA_WINDOW_MARKER}")"
  fi

  if [[ -n "${main_window_id}" && -n "${camera_window_id}" ]]; then
    main_window_line="$(window_line "${main_window_id}")"
    camera_window_line="$(window_line "${camera_window_id}")"
    if [[ "${main_window_line}" != *"${MAIN_WINDOW_MARKER}"* ]]; then
      desired_camera_state="hidden"
    elif [[ "${camera_window_line}" == *"${CAMERA_FULLSCREEN_MARKER}"* ]]; then
      desired_camera_state="fullscreen"
    else
      desired_camera_state="split"
    fi

    if [[ "${desired_camera_state}" != "${camera_window_state}" ]]; then
      case "${desired_camera_state}" in
        split) show_camera_split "${camera_window_id}" ;;
        fullscreen) show_camera_fullscreen "${camera_window_id}" ;;
        hidden) hide_camera_window "${camera_window_id}" ;;
      esac
      camera_window_state="${desired_camera_state}"
    fi
  fi
  sleep 0.25
done
