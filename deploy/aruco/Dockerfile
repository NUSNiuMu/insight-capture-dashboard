FROM ros:humble-ros-base-jammy
RUN apt-get update && apt-get install -y --no-install-recommends python3-pip ffmpeg ros-humble-tf2-ros ros-humble-rmw-cyclonedds-cpp && rm -rf /var/lib/apt/lists/*
RUN pip3 install --no-cache-dir 'numpy>=1.26,<2' 'opencv-contrib-python-headless==4.11.0.86' 'scipy>=1.10,<1.16' 'pyarrow>=17,<18' 'aiohttp>=3.13,<4' 'pyserial==3.5' 'PyYAML>=6,<7'
WORKDIR /app
COPY insight_capture /app/insight_capture
COPY config/devices/x86-aruco /app/config/devices/x86-aruco
CMD ["python3", "-m", "insight_capture.aruco_capture.app"]
