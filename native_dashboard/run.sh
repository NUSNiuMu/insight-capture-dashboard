#!/usr/bin/env bash
set -euo pipefail
native_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
build_dir="${INSIGHT_NATIVE_BUILD_DIR:-$(dirname -- "$native_dir")/.native-build}"
qt_lib="$build_dir/runtime/usr/lib/aarch64-linux-gnu"
if [[ ! -x "$build_dir/insight-native" || ! -d "$qt_lib" ]]; then
    "$native_dir/build.sh"
fi
if [[ $# -eq 0 ]]; then
    set -- --fps 30 --fullscreen
fi
export LD_LIBRARY_PATH="$qt_lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export QT_PLUGIN_PATH="$qt_lib/qt6/plugins"
export QML2_IMPORT_PATH="$qt_lib/qt6/qml"
export QT_QPA_PLATFORM=xcb
export DISPLAY="${DISPLAY:-:0}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [[ -z "${XAUTHORITY:-}" && -f "$XDG_RUNTIME_DIR/gdm/Xauthority" ]]; then
    export XAUTHORITY="$XDG_RUNTIME_DIR/gdm/Xauthority"
fi
exec "$build_dir/insight-native" "$@"
