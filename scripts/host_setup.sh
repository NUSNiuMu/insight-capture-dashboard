#!/usr/bin/env bash
# One-time host-level tuning shared by both deployment paths: the developer
# path (scripts/setup_host.sh, full source checkout) and the end-user path
# (this same file is bundled into the deploy package by build_release.sh, see
# docs/DEPLOYMENT.md §3.2) -- both need identical host OS setup regardless of
# whether the image was built locally or imported from a tarball.
# Idempotent: safe to re-run any time (e.g. after a JetPack reflash).
#
# What it does:
#   1. write /etc/sysctl.d/99-dds-rx-buffers.conf (needs sudo) -- without
#      this, best-effort image samples (~510KB each) overflow the kernel's
#      208KB default UDP receive buffer and recordings silently lose
#      10-24% of image frames (verified 2026-07-07, see docs/USAGE.md)
#   2. install + enable the boot-time camera reboot unit (cameras boot
#      faster than the Jetson and come up with stale DDS participants;
#      see scripts/systemd/insight-camera-reboot.service). The unit's
#      WorkingDirectory/ExecStart are rewritten to this checkout's actual
#      path at install time (not hardcoded in the source unit file), so the
#      same unit works whether this lands in a dev clone or a deploy bundle.
#
# Usage: ./scripts/host_setup.sh

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
        && [[ "$(sysctl -n net.core.wmem_max)" -ge 67108864 ]]; then
    log "sysctl buffers already configured: OK"
else
    log "writing ${SYSCTL_FILE} (sudo password may be prompted)..."
    sudo tee "${SYSCTL_FILE}" >/dev/null <<'EOF'
# DDS large-image paths (insight cameras -> dashboard, and bag playback).
# A single infra frame is a 510KB best-effort sample; the 208KB kernel
# default buffers silently drop them on BOTH sides:
# - receive (rmem): incoming camera frames overflow under CPU bursts and
#   recordings lose 10-24% of image frames (verified 2026-07-07)
# - send (wmem): `ros2 bag play` publishes from THIS host, and its bursts
#   overflow the send buffer -- playback delivered 4-8fps instead of the
#   recorded 20/30fps until this was raised (verified 2026-07-12; live
#   view was never affected because live frames are SENT by the cameras,
#   not by this host)
# FastDDS uses the kernel defaults when no XML override is set, so the
# *_default values matter, not just *_max.
# Written by scripts/host_setup.sh -- re-run it after a reflash.
net.core.rmem_max = 67108864
net.core.rmem_default = 67108864
net.core.wmem_max = 67108864
net.core.wmem_default = 67108864
# IP fragment reassembly headroom for the fragmented UDP datagrams.
net.ipv4.ipfrag_high_thresh = 134217728
EOF
    sudo sysctl -p "${SYSCTL_FILE}"
    log "sysctl buffers: applied + persisted"
fi

# ── boot-time camera reboot unit ─────────────────────────────────────────────
# Cameras power on with the Jetson but boot faster, so their DDS participants
# bind before the Jetson's USB links exist and never recover -- see the
# comment header in scripts/systemd/insight-camera-reboot.service.
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
# docker-compose.yml's cpus limit assumes all 6 Orin NX cores are online.
# nvpmodel can leave the device in a lower-power mode (e.g. 15W = 4 cores)
# from a previous provisioning step, which docker's CPU cgroup rejects
# outright at container-create time ("range of CPUs is from 0.01 to 4.00")
# rather than just running slower -- confirmed 2026-07-12 on a freshly
# flashed unit. Not auto-fixed here (changing power mode needs a reboot,
# too disruptive to do silently); just warn.
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
