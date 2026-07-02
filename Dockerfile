# Insight Capture — Web Dashboard
#
# Base: ROS2 Humble on Ubuntu 22.04 (arm64, matches Jetson Orin JetPack 6.x)
# COLMAP is NOT baked in — it's mounted from the host at runtime because it
# was custom-compiled with CUDA sm_87 for Jetson Orin (see docker-compose.yml).
#
# Build context is the insight_capture project directory:
#   docker build -t insight-dashboard .
# Or use docker-compose (recommended):
#   docker compose up --build

FROM ros:humble-ros-base-jammy

ARG DEBIAN_FRONTEND=noninteractive

# Bake the ros2 CLI onto PATH at the image level (not just via
# docker_entrypoint.sh / ~/.bashrc sourcing setup.bash). VS Code Dev
# Containers' remoteEnv sets ROS_DOMAIN_ID/PYTHONPATH/etc. for its own
# terminals and spawned processes, but NOT PATH, and postCreateCommand only
# patches ~/.bashrc (which non-interactive/subprocess contexts don't source).
# Without this, any code that shells out to `ros2` (e.g.
# post_processing.py's topic discovery and `ros2 bag record`) silently
# fails with FileNotFoundError when launched from a VS Code terminal/task,
# even though rclpy imports (PYTHONPATH-based) keep working fine.
ENV PATH="/opt/ros/humble/bin:${PATH}"

# ── Swap default apt mirrors for Tsinghua (much faster from this network) ───
RUN sed -i 's|http://ports.ubuntu.com/ubuntu-ports/|https://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports/|g' /etc/apt/sources.list \
    && sed -i 's|http://packages.ros.org/ros2/ubuntu|https://mirrors.tuna.tsinghua.edu.cn/ros2/ubuntu|g; s|^Types: deb deb-src|Types: deb|' /etc/apt/sources.list.d/ros2.sources

# ── System & ROS2 packages ──────────────────────────────────────────────────
# NOTE: README.md documents running the PyQt5 kiosk scripts
# (multi_camera_dashboard_qt.py / web_3d_window.py via open_monitor_dashboard.sh
# / open_web_3d_right.sh) from *inside* this container (VS Code Dev Containers
# forwards DISPLAY/X11 automatically on Linux hosts even though it's not in
# docker-compose.yml/devcontainer.json). So PyQt5/QtWebEngine stay here despite
# the container's own CMD being the headless aiohttp dashboard.
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Python build tools
    python3-pip \
    python3-numpy \
    # Qt dashboard / frameless WebEngine window support
    python3-pyqt5 \
    python3-pyqt5.qtwebengine \
    libatk-bridge2.0-0 \
    libasound2 \
    libgbm1 \
    libgl1 \
    libgtk-3-0 \
    libnss3 \
    libxcomposite1 \
    libxdamage1 \
    libxkbcommon-x11-0 \
    libxrandr2 \
    libxcb-cursor0 \
    libxcb-xinerama0 \
    # ROS2 bag I/O and message types
    ros-humble-rosbag2 \
    ros-humble-rosbag2-py \
    ros-humble-rosbag2-storage-default-plugins \
    ros-humble-sensor-msgs \
    ros-humble-geometry-msgs \
    ros-humble-rosidl-runtime-py \
    # COLMAP runtime dependencies (matches host Ubuntu 22.04 packages)
    libboost-program-options1.74.0 \
    libboost-filesystem1.74.0 \
    libmetis5 \
    libgoogle-glog0v5 \
    libglew2.2 \
    libsqlite3-0 \
    liblz4-1 \
    libceres2 \
    libfreeimage3 \
    # rsync & ssh for rosbag remote sync feature
    rsync \
    openssh-client \
    # Utilities
    curl \
    # iproute2 (`ip`) & iputils-ping (`ping`): needed by scripts/reboot_cameras.sh
    # to discover cameras on 169.254.x.x links and wait for them after reboot.
    # Absent from the ros base image, so these commands silently fail (exit 127)
    # if run inside the container without this.
    iproute2 \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

# ── Python packages not available as apt ────────────────────────────────────
# opencv-contrib-python-headless (not apt's python3-opencv): the Ubuntu 22.04 apt
# build is OpenCV 4.5.4, whose cv2.aruco fails to detect DICT_APRILTAG_36h11 markers
# that the same code detects fine on the host (OpenCV 4.11 via pip) — see
# live_alignment.py. Headless avoids the GTK/X11 shared-lib deps of the full wheel;
# nothing here calls cv2.imshow/highgui.
# (matplotlib was removed — nothing under scripts/ imports it; ~93MB of dead
# weight with its fonttools/pillow/kiwisolver deps.)
RUN pip3 install --no-cache-dir \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    "aiohttp==3.13.3" \
    "opencv-contrib-python-headless==4.11.0.86"

# ── Entrypoint: sources ROS2 and sets library paths ─────────────────────────
COPY scripts/docker_entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# ── Working directory matches docker-compose source mount path
WORKDIR /workspaces/insight_capture

EXPOSE 8765

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "-u", "scripts/multi_camera_dashboard_web.py"]
