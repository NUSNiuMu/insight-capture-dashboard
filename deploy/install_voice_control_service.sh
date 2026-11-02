#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
voice_root="${LOOPER_VOICE_ROOT:-${XDG_DATA_HOME:-${HOME}/.local/share}/looper-voice}"
unit_dir="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
unit_path="${unit_dir}/insight-voice-control.service"
template="${project_root}/deploy/systemd/insight-voice-control.service.in"
install_if_ready=false

case "${1:-}" in
  "") ;;
  --if-ready) install_if_ready=true ;;
  -h|--help)
    echo "usage: $0 [--if-ready]"
    echo "  --if-ready  Skip without error when offline voice assets are not provisioned."
    exit 0
    ;;
  *) echo "Unknown option: $1" >&2; exit 2 ;;
esac

missing=()
PYTHONPATH="${voice_root}/python" python3 -c 'import sherpa_onnx' 2>/dev/null \
  || missing+=("${voice_root}/python (sherpa-onnx runtime)")
for required in \
  "${voice_root}/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/model.int8.onnx" \
  "${voice_root}/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/tokens.txt" \
  "${voice_root}/silero_vad.onnx" \
  "${voice_root}/zh_CN-huayan-medium.onnx" \
  "${voice_root}/zh_CN-huayan-medium.onnx.json"; do
  [[ -e "${required}" ]] || missing+=("${required}")
done
if (( ${#missing[@]} > 0 )); then
  printf 'Required voice asset is missing: %s\n' "${missing[@]}" >&2
  if [[ "${install_if_ready}" == "true" ]]; then
    echo "Offline voice service was not installed because its assets are not provisioned." >&2
    exit 0
  fi
  exit 1
fi

mkdir -p "${unit_dir}"
temporary_unit="$(mktemp)"
trap 'rm -f "${temporary_unit}"' EXIT
sed \
  -e "s|@PROJECT_ROOT@|${project_root}|g" \
  -e "s|@VOICE_ROOT@|${voice_root}|g" \
  "${template}" >"${temporary_unit}"
install -m 0644 "${temporary_unit}" "${unit_path}"

systemctl --user daemon-reload

systemctl --user enable insight-voice-control.service
systemctl --user restart insight-voice-control.service
systemctl --user --no-pager --full status insight-voice-control.service
