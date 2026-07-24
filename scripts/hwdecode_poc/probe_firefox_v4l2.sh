#!/usr/bin/env bash

set -euo pipefail

FIREFOX_ROOT="${FIREFOX_ROOT:-/opt/firefox}"
FIREFOX_BIN="${FIREFOX_ROOT}/firefox"
LIBXUL="${FIREFOX_ROOT}/libxul.so"
V4L2TEST="${FIREFOX_ROOT}/v4l2test"

failures=0

pass() {
  printf 'PASS  %s\n' "$1"
}

fail() {
  printf 'FAIL  %s\n' "$1"
  failures=$((failures + 1))
}

info() {
  printf 'INFO  %s\n' "$1"
}

if [[ -x "${FIREFOX_BIN}" ]]; then
  info "$("${FIREFOX_BIN}" --version 2>/dev/null | tail -n 1)"
else
  fail "Firefox binary is missing: ${FIREFOX_BIN}"
fi

if [[ -r "${LIBXUL}" ]] && strings "${LIBXUL}" |
    grep 'Initialising V4L2-DRM FFmpeg decoder' >/dev/null; then
  pass "Firefox contains the V4L2-DRM FFmpeg decoder path"
else
  fail "Firefox does not contain the expected V4L2-DRM decoder path"
fi

mapfile -t video_nodes < <(
  find /dev -maxdepth 1 -type c -name 'video*' -print 2>/dev/null | sort
)
if (( ${#video_nodes[@]} == 0 )); then
  fail "No standard /dev/video* V4L2 node is visible"
else
  pass "Found standard V4L2 nodes: ${video_nodes[*]}"
fi

probe_ok=0
if [[ -x "${V4L2TEST}" ]]; then
  for node in "${video_nodes[@]}"; do
    output="$("${V4L2TEST}" -d "${node}" 2>&1 || true)"
    if grep -q '^OK' <<<"${output}"; then
      pass "Firefox v4l2test accepts ${node}"
      probe_ok=1
    else
      info "Firefox v4l2test rejects ${node}: ${output//$'\n'/; }"
    fi
  done
else
  fail "Firefox v4l2test is missing: ${V4L2TEST}"
fi
if (( probe_ok == 0 )); then
  fail "No visible V4L2 node passed Firefox v4l2test"
fi

for node in /dev/v4l2-nvdec /dev/nvhost-nvdec; do
  if [[ -e "${node}" ]]; then
    info "$(ls -l "${node}")"
    if [[ -x "${V4L2TEST}" ]]; then
      output="$("${V4L2TEST}" -d "${node}" 2>&1 || true)"
      info "Direct NVIDIA-node probe: ${output//$'\n'/; }"
    fi
  fi
done

decoder_fds=()
while IFS= read -r process_dir; do
  pid="${process_dir#/proc/}"
  comm="$(cat "${process_dir}/comm" 2>/dev/null || true)"
  case "${comm}" in
    firefox|firefox-bin|RDD\ Process|WebKitWebProcess)
      while IFS= read -r fd; do
        target="$(readlink "${fd}" 2>/dev/null || true)"
        case "${target}" in
          /dev/video*|/dev/v4l2-nvdec|/dev/nvhost-nvdec)
            decoder_fds+=("${pid}:${comm}:${target}")
            ;;
        esac
      done < <(find "${process_dir}/fd" -maxdepth 1 -type l -print 2>/dev/null)
      ;;
  esac
done < <(find /proc -maxdepth 1 -type d -regex '/proc/[0-9]+' -print 2>/dev/null)

if (( ${#decoder_fds[@]} > 0 )); then
  pass "Browser decoder device FDs: ${decoder_fds[*]}"
else
  fail "No running Firefox/RDD/WebKit process has a decoder device open"
fi

if (( failures > 0 )); then
  printf '\nRESULT  unavailable (%d failed prerequisite(s))\n' "${failures}"
  exit 1
fi

printf '\nRESULT  candidate; confirm lower decode CPU and source-rate playback\n'
