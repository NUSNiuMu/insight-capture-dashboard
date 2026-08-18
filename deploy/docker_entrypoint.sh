#!/bin/bash
set -e

# Source ROS2 environment
source /opt/ros/humble/setup.bash

# Add host COLMAP libraries to search path (libceres, libglog built against Jetson CUDA)
export LD_LIBRARY_PATH="/lib:/lib/aarch64-linux-gnu:/usr/local/cuda/lib64:${LD_LIBRARY_PATH}"

exec "$@"
