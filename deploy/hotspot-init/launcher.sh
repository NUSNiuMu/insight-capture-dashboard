#!/usr/bin/env bash
# The builder embeds report.sh immediately after the shebang so damaged payloads can be diagnosed.
set -euo pipefail
archive=$(realpath "$0")
folder=$(dirname "$archive")
if [[ ${1:-} == --help || ${1:-} == -h ]]; then
    printf '安装：sudo bash %s\n硬件检查：bash %s --check\n' "$0" "$0"
    exit 0
fi

unpack() {
    local line
    line=$(awk '/^__INSIGHT_HOTSPOT_PAYLOAD__$/{print NR+1; exit}' "$archive")
    [[ -n $line ]] || { echo '安装包缺少数据段。' >&2; return 1; }
    tail -n +"$line" "$archive" | tar --no-same-owner --no-overwrite-dir -xz -C "$work"
    (cd "$work" && sha256sum --quiet -c SHA256SUMS)
}

if [[ $# -gt 0 ]]; then
    case "$1" in
        --report)
            file=$(mktemp "$folder/insight-hotspot-report.XXXXXX.txt" 2>/dev/null || mktemp /tmp/insight-hotspot-report.XXXXXX.txt)
            collect 2>&1 | redact > "$file"
            if [[ $EUID == 0 && -n ${SUDO_UID:-} ]]; then chown "$SUDO_UID:${SUDO_GID:-0}" "$file"; fi
            printf '排查报告：%s\n' "$file"
            ;;
        --check)
            work=$(mktemp -d)
            trap 'rm -rf -- "$work"' EXIT
            unpack
            bash "$work/setup.sh" "$@"
            ;;
        *) echo "用法：sudo bash $0" >&2; exit 2 ;;
    esac
    exit 0
fi

umask 027
if [[ $EUID == 0 ]] && install -d -m 0711 /var/log/insight-hotspot &&
        dir=$(mktemp -d /var/log/insight-hotspot/run.XXXXXX); then
    chgrp "${SUDO_GID:-0}" "$dir"
    chmod 2750 "$dir"
else
    dir=$(mktemp -d /tmp/insight-hotspot.XXXXXX)
fi
uid=${SUDO_UID:-$EUID}
gid=${SUDO_GID:-$(id -g)}
printf '%s %s\n' "$uid" "$gid" > "$dir/owner"
printf '%s/insight-hotspot-result.%s.txt\n' "$folder" "${dir##*.}" > "$dir/destination"

install_and_verify() (
    set -euo pipefail
    stage() { printf '%s\n' "$1" > "$dir/stage"; }
    stage '启动安装'
    [[ $EUID == 0 ]] || { echo "请使用 sudo bash $0"; exit 1; }
    [[ $dir == /var/log/insight-hotspot/* ]] || { echo '无法创建跨重启保存结果的日志目录。'; exit 1; }
    mkdir -p "$state"
    exec 9>"$state/verify.lock"
    flock -x 9
    work=$(mktemp -d)
    trap 'rm -rf -- "$work"' EXIT
    stage '解包及校验'
    unpack
    stage '安装离线依赖、驱动及热点服务'
    (umask 022; bash "$work/setup.sh")
    stage '注册重启后的自动验证'
    install -d "$checks"
    install -m 0644 "$work/report.sh" "$work/verify.sh" "$checks/"
    install -m 0644 "$work/insight-hotspot-verify.service" /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable insight-hotspot-verify.service
    pending="$state/pending-verification"
    if reason=$(bash "$checks/verify.sh" --current); then
        echo '热点检查通过。AP 接口、热点地址、DHCP 服务和手机页面均已就绪。' > "$dir/result.txt"
    else
        printf '热点配置已写入，等待重启验证。请在设备空闲时重启一次。\n当前状态：%s\n' "$reason" > "$dir/result.txt"
    fi
    previous=
    if [[ -s $pending ]]; then previous=$(<"$pending"); fi
    printf '%s\n' "$dir" > "$pending.new"
    mv -fT "$pending.new" "$pending"
    if [[ -n $previous && -d $previous ]]; then
        printf '已重新运行修复包，开机验证结果请查看 %s\n' "$(<"$dir/destination")" > "$previous/result.txt"
        publish_result "$previous" >/dev/null
    fi
    cat "$dir/result.txt"
    publish_result "$dir"
)

exec 3>&1
set +e
install_and_verify 2>&1 | tee -a /dev/fd/3 | redact > "$dir/install.log"
status=$?
set -e
if (( status != 0 )); then
    failure_report "$dir" "$(cat "$dir/stage" 2>/dev/null || echo '启动安装')" "$status"
fi
exit "$status"
__INSIGHT_HOTSPOT_PAYLOAD__
