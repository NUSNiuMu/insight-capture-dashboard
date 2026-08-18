#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
voice_root="${LOOPER_VOICE_ROOT:-${XDG_DATA_HOME:-${HOME}/.local/share}/looper-voice}"

export PYTHONPATH="${voice_root}/python${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 "${project_root}/scripts/openclaw_voice_bridge.py" "$@"
