#!/usr/bin/env bash
set -euo pipefail

[[ -f /etc/nv_tegra_release ]] || exit 0
if (( EUID != 0 )); then
    exec sudo bash "$0"
fi

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
config=/etc/insight-nvfancontrol.conf
# Reuse the installed drop-in path so upgrades replace the previous policy in place.
drop_in=/etc/systemd/system/nvfancontrol.service.d/50-insight-full-speed.conf
temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT

# Preserve the board's thermal sensor settings in the NVIDIA daemon.
awk -f "$here/jetson_fan_profile.awk" /etc/nvfancontrol.conf > "$temporary/config" || {
    echo 'ERROR: cannot derive a temperature-based fan profile from /etc/nvfancontrol.conf' >&2
    exit 1
}

cat > "$temporary/drop-in" <<'EOF'
[Service]
# nvfancontrol otherwise reloads the previous profile and control mode.
ExecStartPre=/usr/bin/rm -f /var/lib/nvfancontrol/status
ExecStart=
ExecStart=/usr/sbin/nvfancontrol -f /etc/insight-nvfancontrol.conf
EOF

if ! cmp -s "$temporary/config" "$config" || ! cmp -s "$temporary/drop-in" "$drop_in"; then
    install -m 0644 "$temporary/config" "$config"
    install -D -m 0644 "$temporary/drop-in" "$drop_in"
    systemctl daemon-reload
    systemctl restart nvfancontrol.service
else
    systemctl start nvfancontrol.service
fi
systemctl enable nvfancontrol.service
