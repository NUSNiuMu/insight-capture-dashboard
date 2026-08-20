#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
report_path="/tmp/insight_system_$(date +%Y%m%d_%H%M%S).json"

cd "${project_root}"
exec python3 scripts/system_doctor.py \
    --verbose \
    --output "${report_path}" \
    "$@"
