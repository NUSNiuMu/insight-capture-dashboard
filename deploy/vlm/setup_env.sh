#!/usr/bin/env bash
# Create the Python environment for the pre-annotation inference service.
#
#   deploy/vlm/setup_env.sh                 # venv at $VENV, packages from requirements.txt
#   deploy/vlm/setup_env.sh --fetch-kernel  # also download the FP8 kernel (FP8 models only)
#
# VENV           default ~/Workspace/.venvs/vlm (put it in deploy/vlm/.env as PYTHON=$VENV/bin/python)
# PIP_INDEX_URL  default https://mirrors.ustc.edu.cn/pypi/simple
# HF_ENDPOINT    used for --fetch-kernel, default https://hf-mirror.com
#
# The FP8 kernel is code that transformers downloads from the Hugging Face Hub
# (kernels-community/finegrained-fp8) on first use. Fetch it once here, or copy
# ~/.cache/huggingface/hub/kernels--kernels-community--finegrained-fp8 from a
# machine that already has it.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
VENV="${VENV:-$HOME/Workspace/.venvs/vlm}"
export UV_INDEX_URL="${PIP_INDEX_URL:-https://mirrors.ustc.edu.cn/pypi/simple}"
command -v uv >/dev/null || { echo "需要 uv：curl -LsSf https://astral.sh/uv/install.sh | sh" >&2; exit 1; }

[ -x "$VENV/bin/python" ] || uv venv --python 3.12 "$VENV"
uv pip install --python "$VENV/bin/python" -r "$here/requirements.txt"
"$VENV/bin/python" -c "
import torch, transformers
assert torch.cuda.is_available(), 'torch 看不到 GPU：检查驱动是否支持 CUDA 13'
print('torch', torch.__version__, '| transformers', transformers.__version__, '|', torch.cuda.get_device_name(0))"

if [ "${1:-}" = "--fetch-kernel" ]; then
  cd "$repo"
  HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}" "$VENV/bin/python" -c "
from transformers.integrations.hub_kernels import lazy_load_kernel
print('FP8 kernel:', lazy_load_kernel('finegrained-fp8'))"
fi
echo "环境就绪：PYTHON=$VENV/bin/python"
