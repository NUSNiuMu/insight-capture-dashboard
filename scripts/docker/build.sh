#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

cd "${ROOT_DIR}"
if docker compose version >/dev/null 2>&1; then
  docker compose build "$@"
elif command -v docker-compose >/dev/null 2>&1; then
  docker-compose build "$@"
else
  docker build -t insight-capture-dashboard:latest "$@" .
fi
