#!/usr/bin/env bash
set -euo pipefail
[[ $EUID == 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# Existing capture hotspots are refreshed by the same installer. A regular
# upgrade preserves the driver and NetworkManager profile and starts no duplicate owner.
exec python3 "$here/install_network_services.py" "$@"
