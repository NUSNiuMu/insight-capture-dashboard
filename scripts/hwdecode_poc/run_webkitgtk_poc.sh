#!/usr/bin/env bash

set -euo pipefail

URL="${1:-http://127.0.0.1:8765/3d?hwdecode_poc=1}"
POC_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export DISPLAY="${DISPLAY:-:0}"
if [[ "$(id -u)" == "0" ]] && id kiosk >/dev/null 2>&1; then
  export HOME="/home/kiosk"
  exec runuser -u kiosk --preserve-environment -- "$0" "$@"
fi

export XDG_RUNTIME_DIR="${INSIGHT_WEBKIT_RUNTIME_DIR:-/tmp/insight-webkit-runtime-$(id -u)}"
export GST_PLUGIN_FEATURE_RANK="${GST_PLUGIN_FEATURE_RANK:-nvv4l2decoder:MAX,avdec_h264:NONE,openh264dec:NONE}"
export GST_DEBUG="${GST_DEBUG:-3,webkit*:6,decodebin*:6,v4l2*:6,nvv4l2decoder:6}"
export GST_DEBUG_FILE="${GST_DEBUG_FILE:-/tmp/insight-webkit-hwdecode-$(id -u).log}"
export GST_DEBUG_DUMP_DOT_DIR="${GST_DEBUG_DUMP_DOT_DIR:-/tmp/insight-webkit-gst-dots-$(id -u)}"
export WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS="${WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS:-1}"

NVIDIA_EGL_VENDOR="/usr/lib/aarch64-linux-gnu/tegra-egl/nvidia.json"
if [[ -z "${__EGL_VENDOR_LIBRARY_FILENAMES:-}" && -r "${NVIDIA_EGL_VENDOR}" ]]; then
  export __EGL_VENDOR_LIBRARY_FILENAMES="${NVIDIA_EGL_VENDOR}"
fi

mkdir -p "${XDG_RUNTIME_DIR}" "${GST_DEBUG_DUMP_DOT_DIR}"
chmod 700 "${XDG_RUNTIME_DIR}"

exec python3 "${POC_ROOT}/webkit_dashboard_poc.py" "${URL}"
