#!/usr/bin/env bash
# Apply shared, idempotent Jetson network and camera boot tuning.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SYSCTL_FILE=/etc/sysctl.d/99-dds-rx-buffers.conf

log() { echo "[host_setup] $*"; }

if [[ -f /.dockerenv ]]; then
    echo "[host_setup] ERROR: run this on the host, not inside the container." >&2
    exit 1
fi

# ── kernel UDP buffers for large DDS image samples (both directions) ─────────
if [[ -f "${SYSCTL_FILE}" ]] \
        && [[ "$(sysctl -n net.core.rmem_max)" -ge 67108864 ]] \
        && [[ "$(sysctl -n net.core.wmem_max)" -ge 67108864 ]] \
        && [[ "$(sysctl -n net.core.netdev_max_backlog)" -ge 8192 ]]; then
    log "sysctl buffers already configured: OK"
else
    log "writing ${SYSCTL_FILE} (sudo password may be prompted)..."
    sudo tee "${SYSCTL_FILE}" >/dev/null <<'EOF'
# Buffer large DDS image samples in both recording and playback directions.
net.core.rmem_max = 67108864
net.core.rmem_default = 67108864
net.core.wmem_max = 67108864
net.core.wmem_default = 67108864
# IP fragment reassembly headroom for the fragmented UDP datagrams.
net.ipv4.ipfrag_high_thresh = 134217728
# Increase NAPI backlog for camera USB-ethernet bursts.
net.core.netdev_max_backlog = 8192
EOF
    sudo sysctl -p "${SYSCTL_FILE}"
    log "sysctl buffers: applied + persisted"
fi

# ── boot-time camera reboot unit ─────────────────────────────────────────────
# Reboot cameras after host links exist to refresh their DDS participants.
UNIT_SRC="${SCRIPT_DIR}/systemd/insight-camera-reboot.service"
UNIT_DST=/etc/systemd/system/insight-camera-reboot.service
if [[ -f "${UNIT_SRC}" ]]; then
    rendered_unit="$(sed \
        -e "s#^WorkingDirectory=.*#WorkingDirectory=${ROOT_DIR}#" \
        -e "s#^ExecStart=.*#ExecStart=${ROOT_DIR}/scripts/reboot_cameras.sh#" \
        "${UNIT_SRC}")"
    if [[ "$(cat "${UNIT_DST}" 2>/dev/null || true)" != "${rendered_unit}" ]]; then
        log "installing boot-time camera reboot unit (root: ${ROOT_DIR})..."
        echo "${rendered_unit}" | sudo tee "${UNIT_DST}" >/dev/null
        sudo systemctl daemon-reload
    fi
    sudo systemctl enable insight-camera-reboot.service >/dev/null 2>&1
    log "camera boot-reboot unit: enabled"
else
    log "WARNING: ${UNIT_SRC} missing; skipping boot-time camera reboot setup."
fi

# ── CPU power mode ────────────────────────────────────────────────────────────
# Warn when the compose CPU limit exceeds the active nvpmodel core count.
if command -v nvpmodel >/dev/null 2>&1; then
    online_cpus="$(nproc)"
    if (( online_cpus < 6 )); then
        log "WARNING: only ${online_cpus}/6 CPU cores online (nvpmodel power mode is below max)."
        log "  If this device has no power/thermal constraint, switch to max performance:"
        log "    sudo nvpmodel -m 0   # MAXN_SUPER -- prompts to confirm a reboot"
        log "  Otherwise, lower docker-compose.yml's cpus limit to match (see its comment)."
    else
        log "CPU power mode: OK (${online_cpus}/6 cores online)"
    fi
fi

log "host setup complete."
