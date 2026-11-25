#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/report.sh"

ready() {
    local radio unit
    radio=$(iw dev capture_ap info 2>/dev/null) && grep -Eq '^[[:space:]]*type AP$' <<< "$radio" ||
        { echo '无线驱动尚未将 capture_ap 切换到 AP 模式'; return 1; }
    [[ $(nmcli --wait 4 -g GENERAL.CONNECTION device show capture_ap 2>/dev/null) == jetson-capture-hotspot ]] ||
        { echo 'NetworkManager 尚未激活采集热点'; return 1; }
    ip -o -4 addr show dev capture_ap | grep -Fq ' 10.77.0.1/24 ' ||
        { echo 'capture_ap 尚未取得 10.77.0.1/24'; return 1; }
    for unit in hotspot isolation dhcp; do
        systemctl is-active --quiet "insight-capture-$unit.service" ||
            { echo "服务 insight-capture-$unit 尚未就绪"; return 1; }
    done
    python3 - <<'PY' || { echo '热点已启动，但采集页面尚未返回 HTTP 200'; return 1; }
from urllib.request import ProxyHandler, build_opener
try:
    with build_opener(ProxyHandler({})).open("http://10.77.0.1:8765/mobile", timeout=4) as response:
        raise SystemExit(response.status != 200)
except OSError:
    raise SystemExit(1)
PY
}

if [[ ${1:-} == --current ]]; then ready; exit; fi
exec 9>"$state/verify.lock"
flock -x 9
pending="$state/pending-verification"
[[ -s $pending ]] || exit 0
dir=$(<"$pending")
deadline=$((SECONDS + 180))
until reason=$(ready); do
    if (( SECONDS >= deadline )); then
        failure_report "$dir" "重启后验证：$reason" 1
        rm -f -- "$pending"
        exit 1
    fi
    sleep 5
done
printf '热点检查通过。AP 接口、热点地址、DHCP 服务和手机页面均已就绪。\n' > "$dir/result.tmp"
mv -fT "$dir/result.tmp" "$dir/result.txt"
publish_result "$dir"
rm -f -- "$pending"
