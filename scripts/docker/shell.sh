#!/usr/bin/env bash
set -euo pipefail

INSIGHT_ROSBAG_DIR="${INSIGHT_ROSBAG_DIR:-/workspace/rosbags}"

set +u
source /opt/ros/humble/setup.bash
set -u

cd /workspace/insight_capture
mkdir -p "${INSIGHT_ROSBAG_DIR}"

if [[ "$#" -gt 0 ]]; then
  exec bash "$@"
fi

exec bash
