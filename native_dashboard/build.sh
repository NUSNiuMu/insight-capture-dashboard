#!/usr/bin/env bash
set -euo pipefail
native_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
checkout_dir="$(dirname -- "$native_dir")"
build_dir="${INSIGHT_NATIVE_BUILD_DIR:-$checkout_dir/.native-build}"
if [[ "$(uname -m)" != aarch64 ]]; then
    echo 'This trial build targets an aarch64 Jetson host.' >&2
    exit 1
fi
mkdir -p "$build_dir/jetson-headers"
jetson_headers="${JETSON_MM_HEADERS:-/usr/src/jetson_multimedia_api/include}"
cp "$jetson_headers/nvbufsurftransform.h" "$jetson_headers/nvbufsurface.h" "$build_dir/jetson-headers/"
if [[ -n "${INSIGHT_NATIVE_BUILD_CONTAINER:-}" ]]; then
    docker exec "$INSIGHT_NATIVE_BUILD_CONTAINER" cmake -S /src/native_dashboard -B /src/.native-build -DCMAKE_BUILD_TYPE=Release
    docker exec "$INSIGHT_NATIVE_BUILD_CONTAINER" cmake --build /src/.native-build -j2
    docker exec "$INSIGHT_NATIVE_BUILD_CONTAINER" python3 /src/native_dashboard/export_runtime.py /src/.native-build/qt-runtime.tar
    docker exec "$INSIGHT_NATIVE_BUILD_CONTAINER" chown -R "$(id -u):$(id -g)" /src/.native-build
else
    docker build -t insight-qt-builder:jammy -f "$native_dir/Dockerfile.build" "$native_dir"
    docker run --rm -v "$checkout_dir:/src" -v "$build_dir:/src/.native-build" insight-qt-builder:jammy \
        bash -c 'cmake -S /src/native_dashboard -B /src/.native-build -DCMAKE_BUILD_TYPE=Release && cmake --build /src/.native-build -j2 && python3 /src/native_dashboard/export_runtime.py /src/.native-build/qt-runtime.tar'
    docker run --rm -v "$build_dir:/out" insight-qt-builder:jammy chown -R "$(id -u):$(id -g)" /out
fi
mkdir -p "$build_dir/runtime"
tar -xf "$build_dir/qt-runtime.tar" -C "$build_dir/runtime"
echo "Built $build_dir/insight-native"
