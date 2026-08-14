#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
voice_root="${LOOPER_VOICE_ROOT:-${XDG_DATA_HOME:-${HOME}/.local/share}/looper-voice}"
unit_dir="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
unit_path="${unit_dir}/looper-openclaw-voice.service"
template="${project_root}/deploy/openclaw-voice.service.in"

openclaw_bin="${HOME}/.openclaw/bin/openclaw"
if [[ ! -x "${openclaw_bin}" ]]; then
  echo "OpenClaw is not installed at ${HOME}/.openclaw/bin/openclaw" >&2
  exit 1
fi
if [[ ! -d "${voice_root}/python/vosk" ]]; then
  echo "Vosk runtime is missing under ${voice_root}/python" >&2
  exit 1
fi
if ! PYTHONPATH="${voice_root}/python" python3 -c 'import sherpa_onnx' 2>/dev/null; then
  echo "sherpa-onnx runtime is missing under ${voice_root}/python" >&2
  exit 1
fi
for required in \
  "${voice_root}/vosk-model-small-en-us-0.15" \
  "${voice_root}/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/model.int8.onnx" \
  "${voice_root}/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/tokens.txt" \
  "${voice_root}/silero_vad.onnx" \
  "${voice_root}/zh_CN-huayan-medium.onnx" \
  "${voice_root}/zh_CN-huayan-medium.onnx.json"; do
  if [[ ! -e "${required}" ]]; then
    echo "Required voice asset is missing: ${required}" >&2
    exit 1
  fi
done

"${openclaw_bin}" config set agents.defaults.models \
  '{"openai/gpt-5.6-luna":{"params":{"fastMode":true}}}' \
  --strict-json --merge
"${openclaw_bin}" config set --batch-json \
  '[{"path":"agents.defaults.model.primary","value":"openai/gpt-5.6-luna"},{"path":"agents.defaults.thinkingDefault","value":"off"},{"path":"agents.defaults.skills","value":[]}]'

mkdir -p "${unit_dir}"
temporary_unit="$(mktemp)"
trap 'rm -f "${temporary_unit}"' EXIT
sed \
  -e "s|@PROJECT_ROOT@|${project_root}|g" \
  -e "s|@VOICE_ROOT@|${voice_root}|g" \
  "${template}" >"${temporary_unit}"
install -m 0644 "${temporary_unit}" "${unit_path}"

systemctl --user daemon-reload
systemctl --user enable --now looper-openclaw-voice.service
systemctl --user --no-pager --full status looper-openclaw-voice.service
