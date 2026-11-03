#!/usr/bin/env bash
# Self-extracting offline Release; build_release.sh appends the payload.
set -euo pipefail

if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
    echo "用法：bash $0 /原安装目录"
    exit 0
fi

install_dir="${1:?请指定安装目录：bash $0 /原安装目录}"
mkdir -p "${install_dir}"
install_dir="$(cd "${install_dir}" && pwd)"
payload_dir="$(mktemp -d)"
trap 'rm -rf "${payload_dir}"' EXIT
payload_line="$(awk '/^__INSIGHT_PAYLOAD__$/{print NR+1; exit}' "$0")"
tail -n +"${payload_line}" "$0" | tar -xz -C "${payload_dir}"
bash "${payload_dir}/update.sh" --install-dir "${install_dir}" "${payload_dir}/images.tar"
exit 0
__INSIGHT_PAYLOAD__
