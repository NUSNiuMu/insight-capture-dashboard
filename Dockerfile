FROM ros:humble-ros-base-jammy

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash-completion \
    build-essential \
    curl \
    git \
    less \
    nano \
    nodejs \
    npm \
    python3-aiohttp \
    python3-numpy \
    python3-opencv \
    python3-pip \
    python3-pyqt5 \
    python3-pyqt5.qtwebengine \
    ros-humble-compressed-image-transport \
    ros-humble-cv-bridge \
    ros-humble-image-transport-plugins \
    ros-humble-rosbag2 \
    ros-humble-rosbag2-storage-mcap \
    tmux \
    tree \
    vim \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace/insight_capture

COPY requirements.txt /tmp/insight_capture_requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/insight_capture_requirements.txt

COPY . /workspace/insight_capture
RUN cd /workspace/insight_capture/web_dashboard && npm run build

ENV ROS_DISTRO=humble
ENV ROS_DOMAIN_ID=20
ENV INSIGHT_ROSBAG_DIR=/workspace/rosbags
ENV PYTHONUNBUFFERED=1

RUN echo "source /opt/ros/humble/setup.bash" >> /root/.bashrc

CMD ["bash", "/workspace/insight_capture/scripts/docker_start_app.sh"]
