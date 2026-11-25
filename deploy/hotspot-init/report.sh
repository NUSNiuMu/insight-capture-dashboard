#!/usr/bin/env bash
# Shared by the .run header and the one-time boot check.
state=/var/lib/insight-capture-hotspot
checks=/usr/local/libexec/insight-hotspot-init

section() {
    printf '\n=== %s ===\n' "$1"
}

collect() {
    section '设备和当前开机'
    date -Is
    hostname
    uname -m
    uname -r
    if [[ -f /etc/nv_tegra_release ]]; then head -n 1 /etc/nv_tegra_release; fi
    uptime -s || true

    section '无线网卡和驱动'
    if command -v lspci >/dev/null; then
        lspci -nnk | grep -EiA3 'Network controller|Wireless' || true
    fi
    if command -v modinfo >/dev/null; then
        if driver="$(modinfo -n rtl8822ce 2>/dev/null)" && [[ -f "$driver" ]]; then
            printf '驱动文件 %s\n' "$driver"
            modinfo -F vermagic "$driver" || true
            sha256sum "$driver" || true
            stat -c '驱动修改时间 %y' "$driver" || true
            if modinfo -p "$driver" | grep -q '^if2name:'; then
                echo '磁盘上的驱动声明了 if2name 参数。是否创建了第二个接口，以 capture_ap 状态为准。'
            else
                echo '磁盘上的驱动缺少 if2name 参数'
            fi
        else
            echo '找不到 RTL8822CE 驱动文件'
        fi
    fi
    for parameter in ifname if2name; do
        file="/sys/module/rtl8822ce/parameters/$parameter"
        if [[ -r "$file" ]]; then
            printf '当前加载的驱动 %s=%s\n' "$parameter" "$(<"$file")"
        fi
    done
    lsmod | grep -E 'rtl8822ce|rtw88' || true
    if command -v rfkill >/dev/null; then rfkill list wifi || true; fi

    section '热点接口和 NetworkManager 连接'
    ip -br link || true
    ip -br -4 addr show dev capture_ap || true
    if command -v iw >/dev/null; then iw dev capture_ap info || true; fi
    if command -v nmcli >/dev/null; then
        nmcli -f DEVICE,TYPE,STATE,CONNECTION device status || true
        nmcli -f connection.id,connection.interface-name,802-11-wireless.ssid,802-11-wireless.mode,802-11-wireless.band,802-11-wireless.channel,ipv4.addresses \
            connection show jetson-capture-hotspot || true
    fi
    if [[ -r /etc/insight-capture-hotspot.conf ]]; then
        grep -E '^(UPLINK|AP_CHANNEL|AP_BAND)=' /etc/insight-capture-hotspot.conf || true
    else
        echo '缺少 /etc/insight-capture-hotspot.conf'
    fi
    if [[ -r /etc/modprobe.d/insight-capture-hotspot.conf ]]; then
        grep -E '^options[[:space:]]+rtl8822ce[[:space:]]' /etc/modprobe.d/insight-capture-hotspot.conf || true
    else
        echo '缺少 /etc/modprobe.d/insight-capture-hotspot.conf'
    fi

    section '热点服务与启动错误'
    systemctl show -p Id,LoadState,UnitFileState,ActiveState,SubState,Result \
        insight-capture-hotspot.service insight-capture-isolation.service \
        insight-capture-dhcp.service NetworkManager.service chrony.service insight-capture-camera-discovery.service || true
    journalctl -b --no-pager -o cat -n 30 -u insight-capture-hotspot.service \
        -u insight-capture-isolation.service -u insight-capture-dhcp.service || true
    journalctl -k -b --no-pager -o cat -n 20 \
        --grep='rtl8822ce|rtw88|cfg80211|rfkill|Unknown symbol|Invalid module|version magic|module verification|Required key' || true

    section '热点离线依赖'
    dpkg --audit || true
    dpkg-query -W -f='${Package} ${db:Status-Status} ${Version}\n' chrony systemd-timesyncd dnsmasq-base iptables iw network-manager wpasupplicant || true
    section '采集页面本机状态'
    if command -v curl >/dev/null; then
        curl --noproxy '*' -fsS --max-time 4 -o /dev/null -w 'HTTP /healthz: %{http_code}\n' \
            http://127.0.0.1:8765/healthz || true
        if ip -o -4 addr show dev capture_ap 2>/dev/null | grep -Fq ' 10.77.0.1/'; then
            curl --noproxy '*' -sS --max-time 4 -o /dev/null -w 'HTTP AP /mobile: %{http_code}\n' \
                http://10.77.0.1:8765/mobile || true
        fi
    fi
}

redact() {
    sed -u -E \
    -e 's/^Wi-Fi 密码：.*/Wi-Fi 密码：[已隐藏]/' \
    -e 's/((psk|password|passphrase)([[:space:]]*[:=][[:space:]]*|[[:space:]]+)).*/\1<已隐藏>/I' \
    -e 's/07310731/<已隐藏>/g'
}

# Keep a local copy across reboots, including if the installer was on removable media.
# Publish a regular text file beside the installer using the invoking user's permissions.
publish_result() {
    local dir=$1 destination uid gid
    local -a as_user=()
    destination=$(<"$dir/destination")
    read -r uid gid < "$dir/owner"
    if [[ $EUID == 0 && $uid != 0 ]]; then
        as_user=(setpriv --reuid="$uid" --regid="$gid" --init-groups)
    fi
    if "${as_user[@]}" /bin/sh -ec '
        file=$1
        tmp=$(mktemp -- "${file%/*}/.insight-result.XXXXXX")
        trap '\''rm -f -- "$tmp"'\'' EXIT
        cat > "$tmp"
        mv -fT -- "$tmp" "$file"
    ' sh "$destination" < "$dir/result.txt"; then
        printf '结果文件：%s\n' "$destination"
    else
        printf '结果文件：%s/result.txt\n' "$dir"
    fi
}

failure_report() {
    local dir=$1 stage=$2 code=$3
    {
        printf '热点修复失败\n阶段：%s\n退出码：%s\n' "$stage" "$code"
        section '本次安装输出'
        cat "$dir/install.log"
        collect || true
    } 2>&1 | redact > "$dir/result.tmp"
    mv -fT "$dir/result.tmp" "$dir/result.txt"
    printf '热点修复失败（%s，退出码 %s）。详细排查报告已生成。\n' "$stage" "$code"
    publish_result "$dir"
}
