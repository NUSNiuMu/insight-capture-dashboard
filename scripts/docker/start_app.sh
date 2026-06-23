#!/usr/bin/env bash
set -euo pipefail

BACKEND_HOST="${BACKEND_HOST:-0.0.0.0}"
BACKEND_PORT="${BACKEND_PORT:-8765}"
INSIGHT_ROSBAG_DIR="${INSIGHT_ROSBAG_DIR:-/workspace/rosbags}"

set +u
source /opt/ros/humble/setup.bash
set -u

cd /workspace/insight_capture
mkdir -p "${INSIGHT_ROSBAG_DIR}"
bash scripts/dev/check_env.sh --fix

exec python3 scripts/multi_camera_dashboard_web.py \
  --host "${BACKEND_HOST}" \
  --port "${BACKEND_PORT}" \
  --rosbag-dir "${INSIGHT_ROSBAG_DIR}"
