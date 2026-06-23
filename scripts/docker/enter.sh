#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="${CONTAINER_NAME:-insight_capture_dashboard}"

docker exec -it "${CONTAINER_NAME}" bash /workspace/insight_capture/scripts/docker/shell.sh
