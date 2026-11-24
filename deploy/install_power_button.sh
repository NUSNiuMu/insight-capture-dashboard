#!/usr/bin/env bash
set -euo pipefail

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root=$(realpath -- "${1:?Specify the installation directory}")
port=${2:-8765}
compatible=/proc/device-tree/compatible
input=/dev/input/by-path/platform-gpio-keys-event
unit=insight-power-button.service
rules=/etc/udev/rules.d/99-insight-power-button.rules

if [[ "$(uname -m)" != aarch64 || ! -r "$compatible" ]] ||
    ! grep -zEqx 'nvidia,p3768-0000\+p3767-000[01](-super)?' "$compatible"; then
    echo "[power button] skipping host without the supported Jetson Orin NX power-key configuration"
    exit 0
fi

if (( EUID != 0 )); then
    exec sudo bash "$here/install_power_button.sh" "$root" "$port"
fi

[[ "$port" =~ ^[1-9][0-9]{0,4}$ ]] && (( port <= 65535 )) || {
    echo "[power button] invalid Dashboard port: $port" >&2
    exit 1
}
[[ -c "$input" ]] || {
    echo "[power button] carrier power-key input is missing: $input" >&2
    exit 1
}
properties=$(udevadm info --query=property --name "$input")
grep -qx 'ID_PATH=platform-gpio-keys' <<< "$properties" || {
    echo "[power button] unexpected carrier input path" >&2
    exit 1
}

lock="$root/config/.capture-upgrade.lock"
if [[ ! -e "$lock" ]]; then
    (umask 022; set -o noclobber; : > "$lock") 2>/dev/null || [[ -f "$lock" ]]
fi
[[ -f "$lock" && ! -L "$lock" ]] || {
    echo "[power button] invalid recording lock: $lock" >&2
    exit 1
}

settings=$(mktemp)
trap 'rm -f -- "$settings"' EXIT
# Quote the path for systemd EnvironmentFile, including paths with whitespace.
escaped_lock=${lock//\\/\\\\}
escaped_lock=${escaped_lock//\"/\\\"}
printf 'INSIGHT_CAPTURE_LOCK="%s"\nINSIGHT_CAPTURE_PORT=%s\n' "$escaped_lock" "$port" > "$settings"

changed=false
install_if_changed() {
    local mode=$1 source=$2 destination=$3
    if [[ -L "$destination" || ! -O "$destination" ]] ||
        [[ "$(stat -c %a -- "$destination")" != "${mode#0}" ]] ||
        ! cmp -s -- "$source" "$destination"; then
        install -D -m "$mode" -- "$source" "$destination"
        changed=true
    fi
}

install_if_changed 0755 "$here/power_button.py" /usr/local/libexec/insight-power-button.py
install_if_changed 0644 "$here/systemd/$unit" "/etc/systemd/system/$unit"
install_if_changed 0644 "$settings" /etc/insight-power-button.conf
if "$changed"; then
    systemctl daemon-reload
fi
if ! systemctl is-enabled --quiet "$unit"; then
    systemctl enable "$unit"
fi
if "$changed" || ! systemctl is-active --quiet "$unit"; then
    systemctl restart "$unit"
fi

native_key_excluded() {
    grep -qx 'LIBINPUT_IGNORE_DEVICE=1' <<< "$properties" &&
        ! grep -Eq '^CURRENT_TAGS=.*:power-switch:' <<< "$properties"
}

# Only suppress native handling after the service has grabbed the input.
changed=false
install_if_changed 0644 "$here/udev/99-insight-power-button.rules" "$rules"
if "$changed" || ! native_key_excluded; then
    udevadm control --reload-rules
    event=$(basename "$(realpath -- "$input")")
    udevadm trigger --action=change --settle "/sys/class/input/$event"
    properties=$(udevadm info --query=property --name "$input")
fi
if ! native_key_excluded; then
    echo "[power button] carrier input was not excluded from native short-press handling" >&2
    exit 1
fi
systemctl is-active --quiet "$unit"
echo "[power button] enabled: hold for 2 seconds to request a normal shutdown"
