#!/usr/bin/env bash
# Pre-annotation inference service (insight_capture.vlm_service) for the aruco workbench.
# Runs on a GPU machine; Qwen2.5-VL-7B needs ~17 GB VRAM.
#
#   deploy/vlm/vlm_service.sh start     # start in the background, wait until /health answers
#   deploy/vlm/vlm_service.sh stop
#   deploy/vlm/vlm_service.sh restart
#   deploy/vlm/vlm_service.sh status
#   deploy/vlm/vlm_service.sh logs      # follow the log
#   deploy/vlm/vlm_service.sh run       # foreground (systemd, debugging)
#
# Settings come from the environment or deploy/vlm/.env (see vlm.env.example):
#   MODEL   Qwen2.5-VL weights directory (required)
#   PYTHON  interpreter with torch (CUDA), transformers>=4.49, opencv-python, numpy
#   HOST / PORT   listen address, default 0.0.0.0:8780
# Log and pid file: ~/.local/state/insight-vlm-service/ (STATE_DIR overrides).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
if [ -f "$here/.env" ]; then set -a; . "$here/.env"; set +a; fi
PYTHON="${PYTHON:-python3}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8780}"
state="${STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/insight-vlm-service}"
pid_file="$state/service.pid"
log_file="$state/service.log"
health="http://127.0.0.1:$PORT/health"

running() { [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; }
build_command() {
  : "${MODEL:?未设置 MODEL（Qwen2.5-VL 权重目录），见 deploy/vlm/vlm.env.example}"
  [ -d "$MODEL" ] || { echo "模型目录不存在：$MODEL" >&2; exit 1; }
  cmd=("$PYTHON" -u -m insight_capture.vlm_service.server --model "$MODEL" --host "$HOST" --port "$PORT")
}

case "${1:-}" in
  run)
    build_command
    cd "$repo"
    exec "${cmd[@]}"
    ;;
  start)
    if running; then echo "已在运行（pid $(cat "$pid_file")）"; exit 0; fi
    if curl -fsS -m 2 "$health" >/dev/null 2>&1; then
      echo "端口 $PORT 上已有推理服务在运行（不是本脚本启动的），未重复启动"; exit 1
    fi
    build_command
    mkdir -p "$state"
    cd "$repo"
    nohup setsid "${cmd[@]}" >>"$log_file" 2>&1 &
    echo $! >"$pid_file"
    echo "启动中（pid $!），日志 $log_file"
    for _ in $(seq 1 180); do
      if curl -fsS -m 2 "$health" >/dev/null 2>&1; then
        echo "已就绪：http://$HOST:$PORT  $(curl -fsS -m 2 "$health")"; exit 0
      fi
      running || { echo "启动失败，日志末尾：" >&2; tail -n 20 "$log_file" >&2; rm -f "$pid_file"; exit 1; }
      sleep 1
    done
    echo "3 分钟内未就绪，请查看日志 $log_file" >&2; exit 1
    ;;
  stop)
    if ! running; then echo "未在运行"; rm -f "$pid_file"; exit 0; fi
    pid="$(cat "$pid_file")"
    kill -- "-$pid" 2>/dev/null || kill "$pid"
    for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
    kill -0 "$pid" 2>/dev/null && kill -9 -- "-$pid" 2>/dev/null || true
    rm -f "$pid_file"
    echo "已停止"
    ;;
  restart)
    "$0" stop
    "$0" start
    ;;
  status)
    if running; then echo "运行中（pid $(cat "$pid_file")）"; else echo "未在运行"; fi
    curl -fsS -m 2 "$health" 2>/dev/null && echo || echo "健康检查无响应：$health"
    ;;
  logs)
    exec tail -n 50 -f "$log_file"
    ;;
  *)
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
