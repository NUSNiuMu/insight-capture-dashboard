#!/usr/bin/env bash
# Install or upgrade a customer deployment.
#
# Usage:
#   ./update.sh --install-dir /path/to/installation /tmp/images.tar
#
# Refuses to restart while a recording is in progress (override: --force).
# Superseded Release images and packages are removed after successful startup.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UPDATER_DIR="$SCRIPT_DIR"
if [[ -f "$SCRIPT_DIR/deploy/release_compose.py" ]]; then UPDATER_DIR="$SCRIPT_DIR/deploy"; fi
PORT="${DASHBOARD_PORT:-8765}"
IMAGE_NAME="insight-dashboard"
SUPERGLUE_IMAGE="insight-superglue-validation:25.04"


log() { echo "[$(date '+%H:%M:%S')] $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

acquire_upgrade_lock() {
    [[ -n "${upgrade_lock_fd:-}" ]] && return
    # First installs must seed config/ from the image before creating the lock.
    [[ -d config ]] || return 0
    local lock_file=config/.capture-upgrade.lock
    if [[ ! -e "${lock_file}" ]]; then
        (umask 022; set -o noclobber; : > "${lock_file}") 2>/dev/null \
            || [[ -e "${lock_file}" ]] || die "cannot create the upgrade lock"
    fi
    exec {upgrade_lock_fd}<"${lock_file}"
    flock -n -x "${upgrade_lock_fd}" \
        || die "a recording start or another upgrade is in progress; wait for it to finish"
    # Keep this descriptor open until installer exit, including health checks.
    # The empty lock file remains; process exit releases the actual kernel lock.
}

assert_capture_idle() {
    local response verdict container state
    verdict=unknown
    if response="$(curl --connect-timeout 2 --max-time 5 -sf "http://localhost:${PORT}/api/recording/status" 2>/dev/null)"; then
        verdict="$(python3 -c '
import json, sys
try:
    s = json.load(sys.stdin)
    if not isinstance(s, dict) or not isinstance(s.get("recording"), bool):
        raise ValueError("invalid recording status")
    busy = (s["recording"] or s.get("operation_in_progress")
            or s.get("state") in {"starting", "preparing", "saving", "finalizing", "merging", "stopping"}
            or s.get("merge_state") in {"saving", "finalizing", "merging"}
            or (s.get("current_take") or {}).get("state") in {"preparing", "recording", "saving"})
    print("busy" if busy else "idle" if s.get("upgrade_lock_supported") else "legacy-idle")
except (ValueError, TypeError, AttributeError):
    print("unknown")
' <<<"${response}")"
    fi
    if [[ "${verdict}" == busy ]]; then
        [[ "${force}" == true ]] \
            || die "a recording is in progress (including preparation or saving); finish it before upgrading"
        log "WARNING: --force explicitly permits interrupting the current recording operation."
    elif [[ "${verdict}" == unknown ]]; then
        container="$(docker ps --all --quiet --filter 'name=^/insight-dashboard$')" || die "cannot determine the capture backend state"
        if [[ -n "$container" ]]; then
            state="$(docker inspect --format '{{.State.Status}} {{.State.Pid}}' "$container")" || die "cannot inspect the capture backend; retry before replacing it"
            case "$state" in
                'restarting 0'|'exited 0'|'created 0'|'dead 0')
                    log "Previous Dashboard has no live process ($state); continuing the clean installation."
                    ;;
                *)
                    [[ "${force}" == true ]] || die "the capture backend may still be running but its recording status is unavailable; retry after it responds"
                    log "WARNING: --force proceeds without knowing the running backend's recording state."
                    ;;
            esac
        fi
    elif [[ "${verdict}" == legacy-idle ]]; then
        log "WARNING: the existing backend predates upgrade/start locking; do not start capture until this upgrade exits."
    fi
}

retire_user_camera_profile_service() {
    local unit=insight-capture-camera-profiles.service
    local login_user uid
    local -a user_systemctl=(systemctl --user)
    if (( EUID == 0 )); then
        login_user="${SUDO_USER:-$(stat -c %U "$SCRIPT_DIR/config")}"
        [[ "$login_user" != root ]] || return 0
        uid="$(id -u "$login_user")"
        user_systemctl=(runuser -u "$login_user" -- env XDG_RUNTIME_DIR="/run/user/$uid" systemctl --user)
    fi
    if "${user_systemctl[@]}" is-active --quiet "$unit" 2>/dev/null ||
       "${user_systemctl[@]}" is-enabled --quiet "$unit" 2>/dev/null; then
        log "Retiring the old user camera profile service ..."
        "${user_systemctl[@]}" disable --now "$unit"
    fi
}

runtime_stage=""
runtime_container=""
runtime_switched=false
declare -A runtime_privileged_moves=()
retired_base_files=(compose.yaml compose.yml docker-compose.yaml)
runtime_paths=(
    deploy scripts docker-compose.yml update.sh
    insight_capture/__init__.py
    insight_capture/runtime/discovery.py insight_capture/runtime/camera_profiles.py
    tools/diagnostics/sync_camera_restart.py native_dashboard/run.sh
    .native-build/insight-native .native-build/runtime
)

cleanup_runtime_staging() {
    if [[ -n "$runtime_container" ]]; then
        docker rm "$runtime_container" >/dev/null 2>&1 || true
    fi
    if [[ -n "$runtime_stage" ]]; then
        if "$runtime_switched"; then
            log "Previous runtime files kept in $runtime_stage/previous."
        else
            rm -rf -- "$runtime_stage"
        fi
    fi
}

prepare_runtime_paths() {
    local target destination parent
    for target in "${runtime_paths[@]}"; do
        destination="$SCRIPT_DIR/$target"
        parent="$(dirname "$destination")"
        mkdir -p -- "$parent" 2>/dev/null || sudo mkdir -p -- "$parent"
        # A cross-directory rename also updates '..' inside the old directory.
        # Check that directory itself, even when both parent paths are writable.
        if [[ ! -w "$parent" || ! -x "$parent" ||
              ( -d "$destination" && ! -L "$destination" && ( ! -w "$destination" || ! -x "$destination" ) ) ]]; then
            log "Checking privileged replacement for $target ..."
            sudo test -w "$parent" && sudo test -x "$parent" \
                || die "cannot replace $target in $parent"
            if sudo test -d "$destination" && ! sudo test -L "$destination"; then
                sudo test -w "$destination" && sudo test -x "$destination" \
                    || die "cannot move the old directory $destination"
            fi
            runtime_privileged_moves["$target"]=1
        fi
    done
}

stage_host_runtime() {
    local image="$1" new file
    local compose_selection
    local -a compose_files compose_config
    runtime_stage="$(mktemp -d "$SCRIPT_DIR/.update.XXXXXX")"
    trap cleanup_runtime_staging EXIT
    new="$runtime_stage/new"
    mkdir -p "$new/"{insight_capture/runtime,tools/diagnostics,native_dashboard,.native-build/runtime}
    runtime_container="$(docker create "$image")"

    docker cp "$runtime_container:/workspaces/insight_capture/deploy" "$new/deploy"
    docker cp "$runtime_container:/workspaces/insight_capture/scripts" "$new/scripts"
    docker cp "$runtime_container:/workspaces/insight_capture/insight_capture/__init__.py" "$new/insight_capture/"
    docker cp "$runtime_container:/workspaces/insight_capture/insight_capture/runtime/discovery.py" "$new/insight_capture/runtime/"
    docker cp "$runtime_container:/workspaces/insight_capture/insight_capture/runtime/camera_profiles.py" "$new/insight_capture/runtime/"
    docker cp "$runtime_container:/workspaces/insight_capture/tools/diagnostics/sync_camera_restart.py" "$new/tools/diagnostics/"
    docker cp "$runtime_container:/workspaces/insight_capture/config/devices" "$new/devices"
    docker cp "$runtime_container:/opt/insight/native" "$new/native"
    docker cp "$runtime_container:/workspaces/insight_capture/native_dashboard/run.sh" "$new/native_dashboard/"
    docker rm "$runtime_container" >/dev/null
    runtime_container=""

    install -m 0644 "$new/deploy/docker-compose.yml" "$new/docker-compose.yml"
    # Keep the installer used for this upgrade when reusing an already built image.
    install -m 0755 "$UPDATER_DIR/update.sh" "$new/deploy/update.sh"
    install -m 0644 "$UPDATER_DIR/release_compose.py" "$new/deploy/release_compose.py"
    install -m 0755 "$UPDATER_DIR/update.sh" "$new/update.sh"
    install -m 0755 "$new/native/insight-native" "$new/.native-build/insight-native"
    tar --touch -xf "$new/native/qt-runtime.tar" -C "$new/.native-build/runtime"

    compose_files=(-f "$new/docker-compose.yml")
    for file in compose.override.yml compose.override.yaml docker-compose.override.yml docker-compose.override.yaml; do
        [[ -f "$file" ]] || continue
        cp -p -- "$file" "$new/$file"
        chmod u+w "$new/$file"
        if (( ${#compose_files[@]} == 2 )); then compose_files+=(-f "$new/$file"); fi
        runtime_paths+=("$file")
    done
    docker run --rm --network none --user "$(id -u):$(id -g)" --entrypoint python3 \
        -v "$new:/candidate" "$image" /candidate/deploy/release_compose.py stage /candidate
    for file in compose.override.yml compose.override.yaml docker-compose.override.yml docker-compose.override.yaml; do
        if [[ -f "$new/$file" ]]; then chmod --reference="$file" "$new/$file"; fi
    done

    if [[ -f .env ]]; then
        (umask 077; awk '!/^[[:space:]]*(export[[:space:]]+)?INSIGHT_VERSION[[:space:]]*=/' .env > "$new/.env")
        chmod --reference=.env "$new/.env"
    else
        (umask 077; : > "$new/.env")
    fi
    chmod u+w "$new/.env"
    printf 'INSIGHT_VERSION=%s\n' "$version" >> "$new/.env"
    if [[ -f .env ]]; then chmod --reference=.env "$new/.env"; fi
    runtime_paths+=(.env)
    for file in "${retired_base_files[@]}"; do
        if [[ -e "$file" || -L "$file" ]]; then runtime_paths+=("$file"); fi
    done

    compose_config=(docker compose --project-directory "$SCRIPT_DIR" --env-file "$new/.env" "${compose_files[@]}")
    compose_selection="$(INSIGHT_VERSION="$version" "${compose_config[@]}" config --environment \
        | sed -n 's/^COMPOSE_FILE=//p')"
    if [[ -n "$compose_selection" && "$compose_selection" != docker-compose.yml && \
          "$compose_selection" != "docker-compose.yml:${compose_files[-1]##*/}" ]]; then
        die "COMPOSE_FILE selects a different project; use the installed Release Compose files before upgrading"
    fi
    next_port="$(INSIGHT_VERSION="$version" "${compose_config[@]}" --profile '*' config --format json \
        | python3 "$UPDATER_DIR/release_compose.py" check "$version")"

    prepare_runtime_paths
    # Add missing device defaults and leave the installed site profiles intact.
    cp -an "$new/devices" "$SCRIPT_DIR/config/" 2>/dev/null ||
        sudo cp -an "$new/devices" "$SCRIPT_DIR/config/"
}

install_staged_runtime() {
    local target previous="$runtime_stage/previous"
    local -a move probe
    local retired_voice_unit="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/insight-voice-control.service"
    runtime_switched=true
    mkdir -p -- "$previous"
    for target in "${runtime_paths[@]}"; do
        move=(mv -fT --)
        probe=(test)
        if [[ -n "${runtime_privileged_moves[$target]:-}" ]]; then
            move=(sudo "${move[@]}")
            probe=(sudo test)
        fi
        if "${probe[@]}" -e "$SCRIPT_DIR/$target" || "${probe[@]}" -L "$SCRIPT_DIR/$target"; then
            mkdir -p -- "$previous/$(dirname "$target")"
            "${move[@]}" "$SCRIPT_DIR/$target" "$previous/$target"
        fi
        if [[ -e "$runtime_stage/new/$target" ]]; then
            "${move[@]}" "$runtime_stage/new/$target" "$SCRIPT_DIR/$target"
        fi
    done

    if [[ -f "$retired_voice_unit" ]]; then
        systemctl --user disable --now insight-voice-control.service \
            || log "WARNING: could not stop the retired voice service."
        mv -- "$retired_voice_unit" "$previous/insight-voice-control.service"
        systemctl --user daemon-reload || log "WARNING: could not reload the user service manager."
    fi
}

delete_superseded_runtime() {
    if ! rm -rf -- "$runtime_stage"; then
        sudo rm -rf -- "$runtime_stage"
    fi
    runtime_stage=""
    runtime_switched=false
}

force=false
tarball=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --force) force=true; shift ;;
        --install-dir) SCRIPT_DIR="$(cd "${2:?--install-dir needs the existing Release directory}" && pwd)"; shift 2 ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) die "unknown option: $1 (see --help)" ;;
        *) tarball="$(realpath -m "$1")"; shift ;;
    esac
done

cd "${SCRIPT_DIR}"
command -v docker >/dev/null || die "docker is not installed"
docker compose version >/dev/null 2>&1 || die "docker compose (v2 plugin) is not available"
command -v flock >/dev/null || die "flock is not installed"
if [[ -z "${DASHBOARD_PORT:-}" ]]; then
    installed_port="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' insight-dashboard 2>/dev/null \
        | sed -n 's/^DASHBOARD_PORT=//p' || true)"
    if [[ "$installed_port" =~ ^[0-9]{1,5}$ ]]; then PORT="$installed_port"; fi
fi

# ── Don't kill an in-flight recording ────────────────────────────────────────
acquire_upgrade_lock
assert_capture_idle

# Keep the previous dependency ID before docker load replaces its stable tag.
previous_inference="$(docker image inspect --format '{{.Id}}' "${SUPERGLUE_IMAGE}" 2>/dev/null || true)"
[[ -n "${tarball}" ]] || die "usage: $0 <image-archive> (see --help)"
[[ -f "${tarball}" ]] || die "file not found: ${tarball}"
log "Loading image from ${tarball} (this can take a few minutes)..."
load_output="$(docker load -i "${tarball}")"
echo "${load_output}"
version="$(sed -n "s/^Loaded image: ${IMAGE_NAME}://p" <<<"${load_output}" | head -1)"
[[ -n "${version}" ]] || die "archive did not contain an ${IMAGE_NAME}:* image"

# The single Release payload contains the inference and native-core images too.
docker image inspect "${SUPERGLUE_IMAGE}" >/dev/null 2>&1 \
    || die "missing ${SUPERGLUE_IMAGE}; use the complete Release installer"

docker image inspect "insight-loopervio:${version}" >/dev/null 2>&1 \
    || die "missing insight-loopervio:${version}; load the matching release archive"

log "Checking the Dashboard image, Fast DDS and Jetson preview encoders ..."
docker run --rm --network none --runtime nvidia -e NVIDIA_VISIBLE_DEVICES=all \
    -e NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics,display "${IMAGE_NAME}:${version}" python3 -c '
import ctypes
from insight_capture.runtime.app import main
from insight_capture.media.jetson_preview import Gst, OverviewEncoder
ctypes.CDLL("libfastrtps.so.2.6")
assert callable(main) and callable(OverviewEncoder)
missing = [name for name in ("nvvidconv", "nvv4l2h264enc", "h264parse") if not Gst.ElementFactory.find(name)]
if missing:
    raise SystemExit("Missing Jetson preview plugins: " + ", ".join(missing))
assert "/workspaces/insight_capture/.native-build/fastdds/lib/" in open("/proc/self/maps").read()
'

# ── Data directories (persist across upgrades) ───────────────────────────────
mkdir -p rosbags outputs runs

# First install: seed config/ from the image. Never overwrite an existing
# config/ -- it holds this site's calibration and settings.
if [[ ! -d config ]]; then
    log "First install: seeding config/ from the image..."
    seed_ctr="$(docker create "${IMAGE_NAME}:${version}")"
    docker cp "${seed_ctr}:/workspaces/insight_capture/config" ./config
    docker rm "${seed_ctr}" >/dev/null
fi

# Image loading can take minutes. Recheck before changing .env, stopping any
# service or migrating settings, including upgrades from pre-locking versions.
stage_host_runtime "${IMAGE_NAME}:${version}"
acquire_upgrade_lock
assert_capture_idle

# Install the shipped time service while the old Dashboard still responds.
if [[ -e /etc/nv_tegra_release ]] && { ! command -v chronyd >/dev/null || ! command -v chronyc >/dev/null; }; then
    log "Installing the offline Chrony package ..."
    as_root=()
    if (( EUID != 0 )); then as_root=(sudo); fi
    apt_opts=()
    clock_package="$runtime_stage/new/deploy/camera-clock/vendor/chrony_4.2-2ubuntu2_arm64.deb"
    if [[ -d "$UPDATER_DIR/offline-debs" ]]; then
        apt_state="$runtime_stage/apt"
        mkdir -p "$apt_state/lists/partial"
        printf 'deb [trusted=yes] file:%s ./\n' "$UPDATER_DIR/offline-debs" > "$apt_state/source.list"
        apt_opts=(-o "Dir::Etc::sourcelist=$apt_state/source.list" -o 'Dir::Etc::sourceparts=-'
                  -o "Dir::State::lists=$apt_state/lists" -o "Dir::Cache::archives=$UPDATER_DIR/offline-debs"
                  -o 'Acquire::Languages=none'
                  -o 'APT::Sandbox::User=root')
        "${as_root[@]}" apt-get "${apt_opts[@]}" update
        clock_package=chrony
    else
        [[ -f "$clock_package" ]] || die "the offline Chrony package is missing from this Release"
    fi
    "${as_root[@]}" env DEBIAN_FRONTEND=noninteractive apt-get "${apt_opts[@]}" \
        --no-download --no-install-recommends -o Dpkg::Options::=--force-confold install -y "$clock_package" \
        || die "offline Chrony installation failed; check the APT dependency error above"
    command -v chronyd && command -v chronyc || die "Chrony tools are unavailable after installation"
fi
assert_capture_idle
retire_user_camera_profile_service

log "Starting ${IMAGE_NAME}:${version} ..."
if [[ -f docker-compose.yml || -f docker-compose.yaml || -f compose.yml || -f compose.yaml ]]; then
    docker compose --profile "*" down --remove-orphans
fi
install_staged_runtime
PORT="$next_port"
export INSIGHT_VERSION="$version"
docker compose --profile '*' config --format json | python3 "$UPDATER_DIR/release_compose.py" check "$version" >/dev/null
log "Migrating installed configuration; calibration and original recordings are retained ..."
docker run --rm --network none --entrypoint python3 \
    -v "${SCRIPT_DIR}:/installation" "${IMAGE_NAME}:${version}" \
    /workspaces/insight_capture/deploy/migrate_release.py /installation
ROOT_DIR="${SCRIPT_DIR}"
if [[ $EUID == 0 ]]; then
    bash "${SCRIPT_DIR}/deploy/install_camera_discovery.sh"
else
    sudo bash "${SCRIPT_DIR}/deploy/install_camera_discovery.sh"
fi
source "${SCRIPT_DIR}/deploy/device_services.sh"
# Keep the phone/API reachable while inference engines initialize.
load_device_services
prepare_recording_bind_source
docker compose up -d --no-deps insight-dashboard
services_started=true
if ! start_device_services; then
    services_started=false
    log "WARNING: one or more localization services are still unavailable; check docker compose logs. The recording API remains available."
fi

log "Waiting for the backend to become healthy on :${PORT} ..."
deadline=$(( SECONDS + 90 ))
until curl --noproxy '*' --max-time 3 -sf "http://localhost:${PORT}/healthz" >/dev/null 2>&1; do
    dashboard_id="$(docker compose ps --all --quiet insight-dashboard)"
    dashboard_state="$(docker inspect --format '{{.State.Status}}' "$dashboard_id" 2>/dev/null || true)"
    if [[ "$dashboard_state" == restarting || "$dashboard_state" == exited || "$dashboard_state" == dead ]] ||
            (( SECONDS >= deadline )); then
        docker logs --tail 100 "$dashboard_id" >&2 || true
        die "Dashboard health check failed (container state: ${dashboard_state:-missing}, port: $PORT). Previous Release files are retained."
    fi
    sleep 2
done

bash "${SCRIPT_DIR}/deploy/install_power_button.sh" "${SCRIPT_DIR}" "${PORT}"
if [[ $EUID == 0 ]]; then
    DASHBOARD_PORT="$PORT" bash "${SCRIPT_DIR}/deploy/install_camera_profiles.sh"
else
    sudo DASHBOARD_PORT="$PORT" bash "${SCRIPT_DIR}/deploy/install_camera_profiles.sh"
fi
bash "${SCRIPT_DIR}/deploy/configure_jetson_fan.sh"

"$services_started" || die "Supporting services did not start. Previous Release files are retained."

log "Removing superseded Release images and installation packages ..."
python3 "$SCRIPT_DIR/deploy/cleanup_release.py" "${SCRIPT_DIR}" "${version}" \
    --installer "${INSIGHT_RELEASE_INSTALLER:-${tarball}}" \
    --previous-inference "${previous_inference}" --services "${device_services[@]}" \
    || log "WARNING: cleanup was incomplete; the new services remain installed."

delete_superseded_runtime

log "Done. Dashboard is running ${IMAGE_NAME}:${version} on port ${PORT}."
log "On the Jetson desktop, launch ./native_dashboard/run.sh."
