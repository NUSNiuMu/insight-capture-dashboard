#!/usr/bin/env bash
# Run in the ROS Humble build container, with no camera access required.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)
set +u
source /opt/ros/humble/setup.bash
set -u
mkdir -p "$root/.native-build/dds-tests"
g++ -std=c++14 -O1 \
    -I/opt/ros/humble/include/fastrtps -I/opt/ros/humble/include/fastcdr \
    "$root/tests/runtime/dds/fragment_regression.cpp" \
    -L/opt/ros/humble/lib -lfastrtps -lfastcdr -pthread \
    -o "$root/.native-build/dds-tests/fragment_regression"
export LD_LIBRARY_PATH="$root/.native-build/fastdds/lib:$LD_LIBRARY_PATH"
for scenario in gap single late ordered gap_only repeat_gap gap_complete; do
    timeout 15s "$root/.native-build/dds-tests/fragment_regression" "$scenario"
done
