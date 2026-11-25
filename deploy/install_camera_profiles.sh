#!/usr/bin/env bash
# Install the host watcher where it can reuse the site's Compose project.
set -euo pipefail
project="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
port="${DASHBOARD_PORT:-8765}"
[[ $port =~ ^[0-9]+$ && $project != *\"* && $project != *\\* && $project != *%* && $project != *$'\n'* ]]
unit=insight-capture-camera-profiles.service
systemctl_cmd=(systemctl)
if [[ ${1:-} == --user && $# == 1 ]]; then
    [[ -w "$project/config" ]] || { echo 'Camera configuration must be writable.' >&2; exit 1; }
    if systemctl is-active --quiet "$unit" || systemctl is-enabled --quiet "$unit"; then
        echo 'Camera profile watcher is already installed as a system service.' >&2
        exit 1
    fi
    loginctl --no-ask-password enable-linger "$(id -un)"
    systemctl_cmd+=(--user)
    path="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$unit"
    install -d "$(dirname "$path")"
    dependencies=
    owner_setting=
    target=default.target
elif [[ $# == 0 && $EUID == 0 ]]; then
    path="/etc/systemd/system/$unit"
    dependencies=$'After=docker.service NetworkManager.service\nWants=docker.service'
    owner_setting="User=$(stat -c %U "$project/config")"
    target=multi-user.target
else
    echo "Usage: $0 --user, or run with sudo to install a system service." >&2
    exit 1
fi
temporary="$(mktemp)"
trap 'rm -f -- "$temporary"' EXIT
cat > "$temporary" <<EOF
[Unit]
Description=Select the matching Insight Capture camera profile
$dependencies

[Service]
$owner_setting
WorkingDirectory="$project"
Environment=DASHBOARD_PORT=$port
ExecStart=/usr/bin/python3 -u -m insight_capture.runtime.camera_profiles watch
Restart=on-failure
RestartSec=3

[Install]
WantedBy=$target
EOF
if ! cmp -s "$temporary" "$path"; then
    install -m 0644 "$temporary" "$path"
    "${systemctl_cmd[@]}" daemon-reload
fi
"${systemctl_cmd[@]}" enable "$unit"
# Restart the watcher after replacing its Python module or changing installation path.
"${systemctl_cmd[@]}" restart "$unit"
