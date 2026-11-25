#!/usr/bin/env bash
set -euo pipefail

die() { printf '热点初始化失败：%s\n' "$*" >&2; exit 1; }

here="$(cd "$(dirname "$0")" && pwd)"
deploy="$here/deploy"
check_only=false
if [[ $# -eq 1 && "$1" == --check ]]; then check_only=true; fi
[[ $# -eq 0 || "$check_only" == true ]] || die '用法：sudo bash 本文件 或 bash 本文件 --check'
if [[ "$check_only" == false && $EUID -ne 0 ]]; then
    die '请使用 sudo bash 运行这个文件。'
fi

kernel="$(uname -r)"
driver="$here/drivers/$kernel/rtl8822ce.ko"
[[ $(uname -m) == aarch64 && -f "$driver" ]] || die "包内没有适配当前架构和内核 $kernel 的热点驱动。"
uplink=
for nic in /sys/class/net/*; do
    name="$(basename "$nic")"
    [[ "$name" != capture_ap && -r "$nic/device/vendor" && -r "$nic/device/device" ]] || continue
    [[ "$(<"$nic/device/vendor")" == 0x10ec && "$(<"$nic/device/device")" == 0xc822 ]] || continue
    uplink="$name"
    break
done
[[ -n "$uplink" ]] || die '当前没有检测到 RTL8822CE 无线网卡。'

# These are the host packages used by the upstream hotspot and discovery scripts.
network_missing=()
for package in dnsmasq-base iproute2 iptables iw kmod network-manager wpasupplicant; do
    installed="$(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true)"
    if [[ "$installed" != installed ]]; then network_missing+=("$package"); fi
done
missing=("${network_missing[@]}")
if [[ $(dpkg-query -W -f='${db:Status-Status}' chrony 2>/dev/null || true) != installed ]]; then missing+=(chrony); fi
printf '检测到内核 %s，RTL8822CE 无线接口 %s。\n' "$kernel" "$uplink"
if (("${#missing[@]}")); then printf '将离线补装依赖：%s\n' "${missing[*]}"; fi
check_driver() {
    local current abi
    current="$(modinfo -n rtl8822ce)" || die '系统找不到原有的 RTL8822CE 驱动。'
    [[ -f "$current" && "$current" == *.ko ]] || die "无法替换原有驱动 $current。"
    abi="$(modinfo -F vermagic "$driver")"
    [[ "$abi" == "$kernel "* && "$(modinfo -F vermagic "$current")" == "$abi" ]] ||
        die "热点驱动与当前内核 $kernel 的 ABI 不匹配。"
}
if command -v modinfo >/dev/null; then check_driver; else echo '安装时将补装 kmod 并核对驱动。'; fi
if [[ "$check_only" == true ]]; then exit 0; fi

# The .run application uses the same lock when admitting a recording or upgrade.
application=
if command -v docker >/dev/null && systemctl is-active --quiet docker; then
    application="$(docker ps --quiet --filter 'name=^/insight-dashboard$')" ||
        die '无法确认采集应用的录制状态。'
fi
if [[ -n "$application" ]]; then
    app_path="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$application")"
    if [[ "$app_path" == /* && -d "$app_path/config" ]]; then
        lock="$app_path/config/.capture-upgrade.lock"
        if [[ ! -e "$lock" ]]; then
            (umask 022; set -o noclobber; : >"$lock") 2>/dev/null || [[ -e "$lock" ]] ||
                die '无法创建录制互斥锁。'
        fi
        exec {lock_fd}<"$lock"
        flock -n -x "$lock_fd" || die '设备正在准备录制或升级，请稍后重试。'
    fi
    if ! python3 - <<'PY'
import json
from urllib.request import urlopen

try:
    with urlopen("http://127.0.0.1:8765/api/recording/status", timeout=5) as reply:
        state = json.load(reply)
except (OSError, ValueError):
    raise SystemExit(1)
busy = {"starting", "preparing", "stopping", "saving", "finalizing", "merging"}
if (state.get("recording") is not False or state.get("operation_in_progress")
        or state.get("state") in busy or state.get("merge_state") in busy):
    raise SystemExit(1)
PY
    then
        die '请结束录制、保存和合包，并确认采集服务状态可查询后重试。'
    fi
fi

if (("${#missing[@]}")); then
    apt_state="$here/apt-state"
    mkdir -p "$apt_state/lists/partial" "$apt_state/archives/partial"
    printf 'deb [trusted=yes] file:%s ./\n' "$here/offline-debs" > "$apt_state/source.list"
    apt_options=(
        -o "Dir::Etc::sourcelist=$apt_state/source.list" -o 'Dir::Etc::sourceparts=-'
        -o "Dir::State::lists=$apt_state/lists" -o "Dir::Cache::archives=$apt_state/archives"
        -o 'Acquire::CompressionTypes::Order::=gz' -o 'Acquire::Languages=none' -o 'APT::Sandbox::User=root'
    )
    apt-get "${apt_options[@]}" update
    install_debs() {
        DEBIAN_FRONTEND=noninteractive apt-get "${apt_options[@]}" -o DPkg::Options::=--force-confold \
            -y --no-install-recommends install "$@"
    }
    # Restore missing Wi-Fi dependencies before asking APT to switch time daemons.
    if ((${#network_missing[@]})); then install_debs "${network_missing[@]}"; fi
    if [[ $(dpkg-query -W -f='${db:Status-Status}' chrony 2>/dev/null || true) != installed ]]; then install_debs chrony; fi
fi

check_driver
bash "$deploy/select_iptables_backend.sh"
systemctl is-active --quiet NetworkManager || systemctl start NetworkManager
nmcli radio wifi on
profile=jetson-capture-hotspot
mac="$(tr -d ':' <"/sys/class/net/$uplink/address" | tr '[:lower:]' '[:upper:]')"
[[ "$mac" =~ ^[0-9A-F]{12}$ ]] || die '无法读取无线网卡的 MAC 地址。'
ssid="JETSON_CAPTURE_${mac: -6}"
password=07310731
existing_password=false
if nmcli -g connection.uuid connection show "$profile" >/dev/null 2>&1; then
    prior_ssid="$(nmcli -e no -g 802-11-wireless.ssid connection show "$profile")"
    ssid="${prior_ssid:-$ssid}"
    prior_psk="$(nmcli --show-secrets -e no -g 802-11-wireless-security.psk connection show "$profile")"
    if [[ -n "$prior_psk" ]]; then
        password="$prior_psk"
        existing_password=true
    fi
fi
INSIGHT_HOTSPOT_PASSWORD="$password" bash "$deploy/install_capture_hotspot.sh" "$driver" "$uplink" "$ssid"
printf '\nWi-Fi 名称：%s\n' "$ssid"
if [[ $existing_password == true ]]; then echo 'Wi-Fi 密码沿用原配置。'; else printf 'Wi-Fi 密码：%s\n' "$password"; fi
echo '手机连接热点后访问 http://10.77.0.1:8765/mobile 。'
