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

# ── Swap default apt mirrors for Tsinghua (much faster from this network) ───
RUN sed -i 's|http://ports.ubuntu.com/ubuntu-ports/|https://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports/|g' /etc/apt/sources.list \
    && sed -i 's|http://packages.ros.org/ros2/ubuntu|https://mirrors.tuna.tsinghua.edu.cn/ros2/ubuntu|g; s|^Types: deb deb-src|Types: deb|' /etc/apt/sources.list.d/ros2.sources

# ── System & ROS2 packages ──────────────────────────────────────────────────
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
    && rm -rf /var/lib/apt/lists/*

# ── Python packages not available as apt ────────────────────────────────────
# opencv-contrib-python-headless (not apt's python3-opencv): the Ubuntu 22.04 apt
# build is OpenCV 4.5.4, whose cv2.aruco fails to detect DICT_APRILTAG_36h11 markers
# that the same code detects fine on the host (OpenCV 4.11 via pip) — see
# live_alignment.py. Headless avoids the GTK/X11 shared-lib deps of the full wheel;
# nothing here calls cv2.imshow/highgui.
RUN pip3 install --no-cache-dir \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    "aiohttp==3.13.3" \
    "matplotlib==3.5.1" \
    "opencv-contrib-python-headless==4.11.0.86"

# ── Entrypoint: sources ROS2 and sets library paths ─────────────────────────
COPY scripts/docker_entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# ── Working directory matches docker-compose source mount path
WORKDIR /workspaces/insight_capture

EXPOSE 8765

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "-u", "scripts/multi_camera_dashboard_web.py"]
