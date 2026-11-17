#!/usr/bin/env bash
set -euo pipefail
[[ $EUID == 0 ]] || { echo '请使用 sudo 运行安装包。' >&2; exit 1; }
[[ $(uname -m) == aarch64 ]] || { echo '此安装包用于 ARM64 Jetson Orin NX。' >&2; exit 1; }
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
target=$(realpath -m "${1:?请指定安装目录}")
# Hold the recorder's existing upgrade lock throughout host dependency changes.
if [[ -d "$target/config" ]]; then
    lock_file="$target/config/.capture-upgrade.lock"
    if [[ ! -e "$lock_file" ]]; then
        (umask 022; set -o noclobber; : > "$lock_file") 2>/dev/null \
            || [[ -e "$lock_file" ]] || exit 1
    fi
    exec {upgrade_lock_fd}<"$lock_file"
    flock -n -x "$upgrade_lock_fd" || { echo '请等待录制启动或安装结束。' >&2; exit 1; }
    export upgrade_lock_fd
fi
python3 - <<'PY'
import urllib.request,json,subprocess,shutil
try:
 with urllib.request.urlopen('http://127.0.0.1:8765/api/recording/status',timeout=4) as r:s=json.load(r)
except Exception:
 if shutil.which('docker') and subprocess.run(['docker','ps','--quiet','--filter','name=^/insight-dashboard$'],capture_output=True,text=True).stdout.strip():raise SystemExit('采集服务状态暂不可读，请稍后再安装。')
else:
 if s.get('recording') or s.get('operation_in_progress') or s.get('state') in {'starting','preparing','saving','finalizing','merging','stopping'}:raise SystemExit('请先结束录制并等待保存完成。')
PY
if [[ ! -f /etc/insight-capture-hotspot.conf && $(uname -r) != 5.15.148-tegra ]]; then
    echo '热点驱动需要 5.15.148-tegra 内核。' >&2
    exit 1
fi
bash "$here/select_iptables_backend.sh" >/dev/null
missing=()
while IFS= read -r name; do
    [[ $(dpkg-query -W -f='${db:Status-Status}' "$name" 2>/dev/null || true) == installed ]] || missing+=("$name")
done < "$here/HOST_PACKAGES"
if (( ${#missing[@]} )); then
    apt_state=$(mktemp -d)
    trap 'rm -rf "$apt_state"' EXIT
    mkdir -p "$apt_state/lists/partial"
    printf 'deb [trusted=yes] file:%s ./\n' "$here/offline-debs" > "$apt_state/source.list"
    apt_options=(-o "Dir::Etc::sourcelist=$apt_state/source.list" -o 'Dir::Etc::sourceparts=-' -o "Dir::State::lists=$apt_state/lists" -o "Dir::Cache::archives=$here/offline-debs" -o 'Acquire::Languages=none' -o 'APT::Sandbox::User=root')
    apt-get "${apt_options[@]}" update
    if DEBIAN_FRONTEND=noninteractive apt-get "${apt_options[@]}" --no-download --no-install-recommends -y install "${missing[@]}"; then
        :
    else
        apt_status=$?
        selected=$(bash "$here/select_iptables_backend.sh")
        [[ "$selected" == legacy ]] || exit "$apt_status"
        # iptables may have been unpacked before Docker's post-install failed.
        DEBIAN_FRONTEND=noninteractive apt-get "${apt_options[@]}" --no-download --no-install-recommends -y install "${missing[@]}"
    fi
    rm -rf "$apt_state"; trap - EXIT
fi
bash "$here/select_iptables_backend.sh" >/dev/null
systemctl enable --now docker.service
if ! docker info --format '{{json .Runtimes}}' | python3 -c 'import json,sys;sys.exit("nvidia" not in json.load(sys.stdin))'; then
    nvidia-ctk runtime configure --runtime=docker
    systemctl restart docker.service
fi
owner=${SUDO_USER:-root}
if [[ $owner != root ]]; then usermod -aG docker "$owner"; fi
mkdir -p "$target"
cores=$(nproc)
if (( cores < 6 )) && [[ ! -f "$target/docker-compose.override.yml" ]]; then
    quota=$(python3 -c 'import sys;print(max(.8,int(sys.argv[1])-.2))' "$cores")
    printf "services:\n  insight-dashboard:\n    deploy:\n      resources:\n        limits:\n          cpus: '%s'\n" "$quota" > "$target/docker-compose.override.yml"
fi
bash "$here/application.run" "$target"
bash "$target/deploy/host_setup.sh"
if [[ ! -f /etc/insight-capture-hotspot.conf ]]; then
    uplink=$(nmcli -t -f DEVICE,TYPE device status | awk -F: '$2=="wifi" && $1!="capture_ap" {print $1;exit}')
    [[ -n $uplink ]]
    suffix=$(tr -d ':' < "/sys/class/net/$uplink/address" | tail -c 7 | tr '[:lower:]' '[:upper:]')
    ssid="JETSON_CAPTURE_${suffix}"
    INSIGHT_HOTSPOT_PASSWORD=07310731 bash "$target/deploy/install_capture_hotspot.sh" "$here/rtl8822ce.ko" "$uplink" "$ssid"
fi
if [[ $owner != root ]]; then chown "$owner:$(id -gn "$owner")" "$target"; fi
printf '\n安装完成。重启设备后使用采集热点，密码：07310731\n手机地址：http://10.77.0.1\n'
printf 'Qt 启动：%s/native_dashboard/run.sh\n' "$target"
