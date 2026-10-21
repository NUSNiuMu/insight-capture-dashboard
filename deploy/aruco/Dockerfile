# syntax=docker/dockerfile:1
FROM ros:humble-ros-base-jammy
# Both mirrors default to mainland-China hosts (where this is deployed);
# override at build time for other networks, e.g.
# --build-arg APT_MIRROR=http://archive.ubuntu.com/ubuntu
# --build-arg PIP_INDEX_URL=https://pypi.org/simple
ARG APT_MIRROR=http://mirrors.aliyun.com/ubuntu
RUN sed -i "s|http://archive.ubuntu.com/ubuntu|${APT_MIRROR}|g; s|http://security.ubuntu.com/ubuntu|${APT_MIRROR}|g" /etc/apt/sources.list
# Cache mounts persist downloaded .deb/wheel files across builds (and across
# a failed build's retry) without bloating the final image: unlike a COPY or
# a plain RUN, anything under a --mount=type=cache path never becomes part
# of the committed layer.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends python3-pip ffmpeg v4l-utils ros-humble-tf2-ros ros-humble-rmw-cyclonedds-cpp
# Not Aliyun for pip: it throttles HTTP/1.1 downloads to ~160 KB/s, and pip
# only speaks HTTP/1.1, so the wheels alone took 20+ minutes.
ARG PIP_INDEX_URL=https://mirrors.ustc.edu.cn/pypi/simple
RUN --mount=type=cache,target=/root/.cache/pip \
    pip3 install --index-url "$PIP_INDEX_URL" 'numpy>=1.26,<2' 'opencv-contrib-python-headless==4.11.0.86' 'scipy>=1.10,<1.16' 'pyarrow>=17,<18' 'aiohttp>=3.13,<4' 'pyserial==3.5' 'PyYAML>=6,<7'
WORKDIR /app
COPY insight_capture /app/insight_capture
COPY web_dashboard/dist/static/babylon.js /app/web_dashboard/dist/static/babylon.js
COPY config/devices/x86-aruco /app/config/devices/x86-aruco
CMD ["python3", "-m", "insight_capture.aruco_capture.app"]
