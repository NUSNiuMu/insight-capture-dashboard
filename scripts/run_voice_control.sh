#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
voice_root="${LOOPER_VOICE_ROOT:-${XDG_DATA_HOME:-${HOME}/.local/share}/looper-voice}"

export PYTHONPATH="${voice_root}/python${PYTHONPATH:+:${PYTHONPATH}}"
cd "${project_root}"
exec python3 -m insight_capture.voice.service "$@"
