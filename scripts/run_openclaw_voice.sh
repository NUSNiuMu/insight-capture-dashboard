#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
voice_root="${LOOPER_VOICE_ROOT:-${XDG_DATA_HOME:-${HOME}/.local/share}/looper-voice}"
alsa_card="${LOOPER_ALSA_CARD:-E3}"
playback_volume="${LOOPER_PLAYBACK_VOLUME:-40%}"

if command -v amixer >/dev/null 2>&1; then
  amixer -q -c "${alsa_card}" sset PCM "${playback_volume}" unmute || true
fi

export PYTHONPATH="${voice_root}/python${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 "${project_root}/scripts/openclaw_voice_bridge.py" "$@"
