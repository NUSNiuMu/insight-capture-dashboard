#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FIX=0
QUIET=0

usage() {
  cat <<'EOF'
Usage: scripts/dev/check_env.sh [--fix] [--quiet]

Checks the host before opening the Dev Container, or the runtime inside it.
With --fix inside the container, rebuilds generated assets only when stale.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --fix)
      FIX=1
      ;;
    --quiet)
      QUIET=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

log() {
  if [[ "${QUIET}" != "1" ]]; then
    echo "$@"
  fi
}

failures=0

require_command() {
  local command_name="$1"
  if command -v "${command_name}" >/dev/null 2>&1; then
    log "[ok] command: ${command_name}"
  else
    echo "[missing] command: ${command_name}" >&2
    failures=$((failures + 1))
  fi
}

require_path() {
  local path="$1"
  if [[ -e "${path}" ]]; then
    log "[ok] path: ${path}"
  else
    echo "[missing] path: ${path}" >&2
    failures=$((failures + 1))
  fi
}

check_python_imports() {
  python3 - <<'PY'
import importlib
import sys

modules = [
    "aiohttp",
    "cv2",
    "numpy",
    "PyQt5",
    "rclpy",
]

missing = []
for module in modules:
    try:
        importlib.import_module(module)
    except Exception as exc:
        missing.append(f"{module}: {exc}")

if missing:
    for item in missing:
        print(f"[missing] python import: {item}", file=sys.stderr)
    raise SystemExit(1)

for module in modules:
    print(f"[ok] python import: {module}")
PY
}

newer_than() {
  local source_dir="$1"
  local target_path="$2"
  find "${source_dir}" -type f -newer "${target_path}" -print -quit | grep -q .
}

web_dist_stale() {
  local web_root="${ROOT_DIR}/web_dashboard"
  local dist_dir="${web_root}/dist"
  local marker="${dist_dir}/static/app.js"

  [[ ! -f "${dist_dir}/index.html" ]] && return 0
  [[ ! -f "${dist_dir}/3d.html" ]] && return 0
  [[ ! -f "${dist_dir}/cameras.html" ]] && return 0
  [[ ! -f "${marker}" ]] && return 0
  [[ ! -f "${dist_dir}/static/styles.css" ]] && return 0
  newer_than "${web_root}/src" "${marker}" && return 0
  [[ "${web_root}/build.js" -nt "${marker}" ]] && return 0
  [[ "${web_root}/package.json" -nt "${marker}" ]] && return 0
  return 1
}

ensure_web_dist() {
  if web_dist_stale; then
    if [[ "${FIX}" == "1" ]]; then
      log "[fix] rebuilding web_dashboard/dist"
      node "${ROOT_DIR}/web_dashboard/build.js"
    else
      echo "[stale] web_dashboard/dist; run scripts/dev/check_env.sh --fix" >&2
      failures=$((failures + 1))
    fi
  else
    log "[ok] web_dashboard/dist is current"
  fi
}

cd "${ROOT_DIR}"

check_host() {
  if command -v docker >/dev/null 2>&1; then
    log "[ok] Docker is installed."

    if groups | grep -qw docker; then
      log "[ok] Current user is in the docker group."
    else
      echo "[missing] Current user is not in the docker group. Try: sudo usermod -aG docker \$USER && newgrp docker" >&2
      failures=$((failures + 1))
    fi

    if docker info >/dev/null 2>&1; then
      log "[ok] Docker daemon is running and accessible."
    else
      echo "[missing] Docker daemon is not running or not accessible. Try: sudo systemctl start docker" >&2
      failures=$((failures + 1))
    fi
  else
    echo "[missing] Docker is not installed. Try: sudo apt-get update && sudo apt-get install -y docker.io" >&2
    failures=$((failures + 1))
  fi

  require_path "${ROOT_DIR}/.devcontainer/devcontainer.json"
}

check_container() {
  require_command bash
  require_command python3
  require_command node
  require_command ros2
  require_path /opt/ros/humble/setup.bash

  set +u
  source /opt/ros/humble/setup.bash
  set -u

  if [[ "${QUIET}" == "1" ]]; then
    if ! check_python_imports >/dev/null; then
      failures=$((failures + 1))
    fi
  elif ! check_python_imports; then
    failures=$((failures + 1))
  fi

  mkdir -p "${INSIGHT_ROSBAG_DIR:-/workspace/rosbags}"
  ensure_web_dist
}

if [[ -f /opt/ros/humble/setup.bash ]]; then
  check_container
else
  check_host
fi

if [[ "${failures}" -gt 0 ]]; then
  echo "Environment check failed with ${failures} issue(s)." >&2
  exit 1
fi

log "Environment check passed."
