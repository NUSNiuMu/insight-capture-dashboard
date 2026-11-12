#!/bin/bash
set -e

# Source ROS2 environment
source /opt/ros/humble/setup.bash

# Load the owned fragment/GAP fix before any ROS library is imported. Recorder
# and preview children inherit this path; the base ROS installation is untouched.
dds_lib=/workspaces/insight_capture/.native-build/fastdds/lib
if [[ ! -r "$dds_lib/libfastrtps.so.2.6" ]]; then
    echo "Insight Fast DDS runtime is missing; rebuild the Insight image or run deploy/build_dds_network.sh in its build container." >&2
    exit 1
fi

# Add host COLMAP libraries to search path (libceres, libglog built against Jetson CUDA)
export LD_LIBRARY_PATH="${dds_lib}:/lib:/lib/aarch64-linux-gnu:/usr/local/cuda/lib64:${LD_LIBRARY_PATH}"

exec "$@"
