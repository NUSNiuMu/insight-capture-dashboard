#!/usr/bin/env bash
# Build the project's owned Fast DDS 2.6.11 fragment/GAP fix without downloads.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
vendor="$root/third_party/fastdds"
work="$root/.native-build/fastdds-build"
runtime="$root/.native-build/fastdds/lib"
mkdir -p "$work" "$runtime"
(cd "$vendor" && sha256sum --check SHA256SUMS)
input_id=$(sha256sum "$vendor/SHA256SUMS" | cut -d ' ' -f 1)
if [[ ! -f "$work/input-id" ]] || [[ $(cat "$work/input-id") != "$input_id" ]]; then
    # This directory contains only this component's generated build inputs.
    rm -rf -- "$work/source" "$work/asio" "$work/build"
    mkdir -p "$work/source" "$work/asio"
    tar -xzf "$vendor/Fast-DDS-2.6.11.tar.gz" --strip-components=1 -C "$work/source"
    tar -xzf "$vendor/asio-b84e6c16.tar.gz" --strip-components=1 -C "$work/asio"
    patch --batch --forward -p1 -d "$work/source" < "$vendor/fix-fragmented-gap.patch"
    printf '%s\n' "$input_id" > "$work/input-id"
fi
# Dependencies are already provided by the project's ROS Humble image.
# Match the installed 2.6 ABI options; do not enable the statistics ABI.
cmake -S "$work/source" -B "$work/build" \
    -DCMAKE_PREFIX_PATH=/opt/ros/humble \
    -DCMAKE_BUILD_TYPE=Release \
    -DTHIRDPARTY=OFF -DAsio_INCLUDE_DIR="$work/asio/asio/include" \
    -DSECURITY=ON -DSQLITE3_SUPPORT=ON -DFASTDDS_STATISTICS=OFF \
    -DBUILD_TESTING=OFF -DCOMPILE_TOOLS=OFF
cmake --build "$work/build" --target fastrtps --parallel "${INSIGHT_BUILD_JOBS:-2}"
install -m 0755 "$work/build/src/cpp/libfastrtps.so.2.6.11" "$runtime/libfastrtps.so.2.6.11.next"
mv "$runtime/libfastrtps.so.2.6.11.next" "$runtime/libfastrtps.so.2.6.11"
ln -sfn libfastrtps.so.2.6.11 "$runtime/libfastrtps.so.2.6"
ln -sfn libfastrtps.so.2.6.11 "$runtime/libfastrtps.so"
printf '%s\n' "$input_id" > "$root/.native-build/fastdds/input-id"
