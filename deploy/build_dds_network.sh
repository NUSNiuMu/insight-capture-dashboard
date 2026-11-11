#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
mkdir -p "$root/.native-build"
g++ -std=c++14 -shared -fPIC \
    -I/opt/ros/humble/include/fastrtps -I/opt/ros/humble/include/fastcdr \
    "$root/insight_capture/runtime/ros/network_transport.cpp" \
    -L/opt/ros/humble/lib -lfastrtps \
    -o "$root/.native-build/libinsight_dds_network.so.next"
mv "$root/.native-build/libinsight_dds_network.so.next" "$root/.native-build/libinsight_dds_network.so"
