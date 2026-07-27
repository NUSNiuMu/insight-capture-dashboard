# Insight Capture — Web Dashboard
#
# Base: ROS2 Humble on Ubuntu 22.04 (arm64, matches Jetson Orin JetPack 6.x)
# jetson-nx only (no Nano support on this branch): COLMAP 3.9.1 is compiled
# from source with CUDA sm_87 in the "colmap-builder" stage below and baked
# into the final image, so the built image is fully self-contained -- no
# per-device custom-compiled binaries or host mounts required. See the
# stage's own comments for why 3.9.1 (not the newer 4.x) and why OpenBLAS
# (not COLMAP's default Intel MKL, which has no ARM64 build).
#
# Build context is the insight_capture project directory:
#   docker build -t insight-dashboard .
# Or use docker-compose (recommended):
#   docker compose up --build

# ── Stage: COLMAP 3.9.1, CUDA sm_87 (Jetson Orin) ───────────────────────────
# Built in its own stage (not the final image) so a rebuild triggered by an
# application-code change doesn't recompile COLMAP -- this layer only
# invalidates when this stage's own instructions change. Expect this stage
# alone to take on the order of an hour on-device the first time; cached
# afterward like any other Docker layer.
FROM ros:humble-ros-base-jammy AS colmap-builder
ARG DEBIAN_FRONTEND=noninteractive

RUN sed -i 's|http://ports.ubuntu.com/ubuntu-ports/|https://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports/|g' /etc/apt/sources.list

# NVIDIA's own arm64 CUDA apt repo (same one already configured on the host
# at /etc/apt/sources.list.d/cuda-ubuntu2204-arm64.list) -- gives this build
# stage `nvcc` without needing the host's CUDA toolkit mounted in, and
# without needing an NVIDIA L4T/CUDA base image. This is NOT Ubuntu's own
# `nvidia-cuda-toolkit` apt package (that one needs a gcc-10 workaround on
# 22.04, documented in COLMAP's install docs) -- NVIDIA's official 12.6
# toolkit builds cleanly against jammy's default gcc-11.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates wget \
    && wget -q https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/arm64/cuda-keyring_1.1-1_all.deb \
    && dpkg -i cuda-keyring_1.1-1_all.deb \
    && rm cuda-keyring_1.1-1_all.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends cuda-toolkit-12-6 \
    && rm -rf /var/lib/apt/lists/*
ENV PATH="/usr/local/cuda-12.6/bin:${PATH}"

# COLMAP 3.9.1's own documented Ubuntu build deps (doc/install.rst in the
# colmap/colmap repo) -- every package here resolves on this host's arm64
# apt (verified with `apt-cache policy` before committing to this version;
# see the discussion in the commit message for why 3.9.1 over the current
# 4.x, which pulls in Qt6/OpenImageIO/ONNX/FAISS/PoseLib via FetchContent --
# untested territory on ARM64 and unneeded for this pipeline's plain
# feature_extractor/sequential_matcher/mapper usage).
RUN apt-get update && apt-get install -y --no-install-recommends \
    git cmake ninja-build build-essential \
    libboost-program-options-dev libboost-filesystem-dev libboost-graph-dev libboost-system-dev \
    libeigen3-dev libflann-dev libfreeimage-dev libmetis-dev \
    libgoogle-glog-dev libgtest-dev libsqlite3-dev libglew-dev \
    qtbase5-dev libqt5opengl5-dev \
    libcgal-dev libceres-dev \
    # COLMAP's CMake defaults to Intel MKL (-DBLA_VENDOR=Intel10_64lp), which
    # has no ARM64 build. OpenBLAS is COLMAP's own documented alternative --
    # the OpenMP variant specifically, per install.rst's warning about a
    # known OpenBLAS/OpenMP interaction otherwise.
    libopenblas-openmp-dev \
    && rm -rf /var/lib/apt/lists/*

# CMAKE_CUDA_ARCHITECTURES=87 (not "all"/"all-major"): this branch only ever
# targets Orin NX's Ampere sm_87, so there's no reason to pay for compiling
# and shipping kernels for every other architecture. GUI_ENABLED=OFF: this
# dashboard only ever shells out to `colmap feature_extractor` /
# `sequential_matcher` / `mapper` (see scripts/post_processing.py), so the
# Qt GUI binary is dead weight -- skipping it cuts real build time.
RUN git clone --branch 3.9.1 --depth 1 https://github.com/colmap/colmap.git /colmap-src \
    && mkdir /colmap-src/build \
    && cd /colmap-src/build \
    && cmake .. -GNinja \
        -DCUDA_ENABLED=ON \
        -DCMAKE_CUDA_ARCHITECTURES=87 \
        -DGUI_ENABLED=OFF \
        -DBLA_VENDOR=OpenBLAS \
        -DCMAKE_INSTALL_PREFIX=/colmap-install \
    # -j2, not ninja's default of one job per core: this device has 6 cores
    # but only ~7.4GB RAM, and COLMAP's heavier translation units (Ceres/
    # CGAL-templated .cc, CUDA .cu) can each need 1-2GB+ -- unbounded
    # parallelism was observed swap-thrashing to near-zero free memory
    # during the actual on-device build. Slower, but doesn't risk the OOM
    # killer taking out the build (or something else on the host) partway
    # through a ~176-object compile.
    && ninja -j2 \
    && ninja install

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
# The legacy Qt dashboard has been removed. The on-device kiosk is the vendored
# Playwright Chromium below, whose runtime libs come from
# `playwright install --with-deps`, not from this list.
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Python build tools
    python3-pip \
    python3-numpy \
    # ROS2 bag I/O and message types
    ros-humble-rosbag2 \
    ros-humble-rosbag2-py \
    ros-humble-rosbag2-storage-default-plugins \
    ros-humble-sensor-msgs \
    ros-humble-geometry-msgs \
    ros-humble-nav-msgs \
    ros-humble-std-msgs \
    ros-humble-tf2-ros \
    ros-humble-sensor-msgs-py \
    ros-humble-rosidl-runtime-py \
    # Detection2DArray for the insight9_a hand-landmark overlay (Settings page)
    ros-humble-vision-msgs \
    # COLMAP runtime dependencies for the binary baked in by the
    # colmap-builder stage above (shared libs only, no -dev headers)
    libboost-program-options1.74.0 \
    libboost-filesystem1.74.0 \
    libflann1.9 \
    libmetis5 \
    libgoogle-glog0v5 \
    libglew2.2 \
    libsqlite3-0 \
    liblz4-1 \
    libceres2 \
    libfreeimage3 \
    libopenblas0-openmp \
    libgomp1 \
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

# ── CUDA runtime for the baked-in COLMAP binary ─────────────────────────────
# Only the runtime libs (libcudart, libcublas, libcufft, ...), not the full
# toolkit with nvcc/headers -- that's the colmap-builder stage's job, and
# keeping it out of the final image saves real space. Kept separate from the
# COLMAP runtime-lib apt block above since it's a different repo/keyring.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates wget \
    && wget -q https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/arm64/cuda-keyring_1.1-1_all.deb \
    && dpkg -i cuda-keyring_1.1-1_all.deb \
    && rm cuda-keyring_1.1-1_all.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends cuda-cudart-12-6 libcublas-12-6 libcufft-12-6 \
    && rm -rf /var/lib/apt/lists/*

# ── COLMAP binary, baked in from the colmap-builder stage ───────────────────
COPY --from=colmap-builder /colmap-install/bin/colmap /usr/local/bin/colmap
COPY --from=colmap-builder /colmap-install/share/colmap /usr/local/share/colmap

# ── looper-vio-colmap-handoff: the optimization pipeline COLMAP feeds into ──
# Sibling of /workspaces/insight_capture, not a subdirectory -- that's the
# path scripts/multi_camera_dashboard_web.py expects (Path(__file__)
# .resolve().parents[2] / "looper-vio-colmap-handoff") and it's outside this
# repo's own git history, so cloning it here (pinned to a commit for
# reproducibility) rather than vendoring it as tracked files.
RUN git clone https://github.com/howardat666/looper-vio-colmap-handoff.git /workspaces/looper-vio-colmap-handoff \
    && git -C /workspaces/looper-vio-colmap-handoff checkout e4267cf181f1db89ca3c5a88d3f0e91e4a80658b

# Two local patches on top of the pinned commit (upstream, not ours to fix
# directly -- re-check both against any future commit bump):
#
# 1. run_colmap_from_images.py passes --FeatureExtraction.use_gpu /
#    --FeatureMatching.use_gpu / --FeatureMatching.guided_matching, which
#    COLMAP 3.9.1 rejects outright ("unrecognised option") -- every GPU-path
#    option actually lives under --SiftExtraction.*/--SiftMatching.* (see
#    `colmap feature_extractor -h` / `colmap sequential_matcher -h`).
#    Verified live against a real bag on 2026-07-08: this is the only naming
#    mismatch across the whole script (mapper's own flags are unaffected).
#
# 2. Wrap the local colmap invocation in `stdbuf -oL -eL`: COLMAP's own C++
#    stdio block-buffers (4KB) instead of line-buffering once stdout isn't a
#    TTY, which it never is here -- it's piped through
#    run_pipeline_from_rosbag.py's subprocess.Popen up to
#    OptimizationManager._monitor(). Without this, the Optimization page's
#    live log and progress bar sit frozen for tens of seconds and then jump
#    in bursts as each buffer flushes, instead of updating line-by-line.
RUN cd /workspaces/looper-vio-colmap-handoff && \
    sed -i \
        -e 's/"--FeatureExtraction\.use_gpu"/"--SiftExtraction.use_gpu"/' \
        -e 's/"--FeatureMatching\.use_gpu"/"--SiftMatching.use_gpu"/' \
        -e 's/"--FeatureMatching\.guided_matching"/"--SiftMatching.guided_matching"/' \
        -e 's/return \[args\.colmap_bin, \*colmap_args\]/return ["stdbuf", "-oL", "-eL", args.colmap_bin, *colmap_args]/' \
        scripts/run_colmap_from_images.py && \
    grep -q "SiftExtraction.use_gpu" scripts/run_colmap_from_images.py && \
    grep -q "SiftMatching.guided_matching" scripts/run_colmap_from_images.py && \
    grep -q 'stdbuf", "-oL", "-eL", args.colmap_bin' scripts/run_colmap_from_images.py

# ── Python packages not available as apt ────────────────────────────────────
# opencv-contrib-python-headless (not apt's python3-opencv): the Ubuntu 22.04 apt
# build is OpenCV 4.5.4, whose cv2.aruco fails to detect DICT_APRILTAG_36h11 markers
# that the same code detects fine on the host (OpenCV 4.11 via pip) — see
# live_alignment.py. Headless avoids the GTK/X11 shared-lib deps of the full wheel;
# nothing here calls cv2.imshow/highgui.
# matplotlib: only looper-vio-colmap-handoff's plot_trajectories.py needs it
# (run_pipeline_from_rosbag.py's --make-plots, currently always "false" from
# post_processing.py -- installed anyway so flipping that flag doesn't
# surface an ImportError from a separate subprocess).
RUN pip3 install --no-cache-dir \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    "aiohttp==3.13.3" \
    "opencv-contrib-python-headless==4.11.0.86" \
    "matplotlib"

# ── Kiosk browser (scripts/open_web_3d_right.sh) ────────────────────────────
# The on-device kiosk previously used PyQt5's QWebEngineView, which bundles
# Chromium 87 (Nov 2020, frozen since). That old build's GPU compositor has
# a bug on this Tegra GPU/driver combo: under high-frequency, large texture
# uploads (the live camera feed panels) it presents a blank/white compositor
# frame -- visible as a flicker, worse the busier the page. Confirmed absent
# on a current Chromium build on the same hardware. Vendored here via
# Playwright rather than the host's snap/apt chromium-browser: both of those
# route through snap-confine, which was found broken (missing file
# capabilities) on at least one deployed Jetson -- vendoring into the image
# sidesteps the host package manager entirely and keeps the browser version
# pinned and reproducible across machines.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
RUN pip3 install --no-cache-dir \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    "playwright==1.61.0" \
    && python3 -m playwright install --with-deps chromium

# ── Kiosk browser, take two: official Firefox (scripts/open_web_3d_right.sh) ─
# The Playwright Chromium above is no longer used as the on-device kiosk --
# kept only for headless page verification (screenshots, console-error
# checks; see CLAUDE.md), since it has no H.264 decoder at all (checked via
# RTCRtpReceiver.getCapabilities: VP8/VP9/AV1 only), so it can never show
# the WebRTC camera streams and was permanently stuck on the JPEG-polling
# fallback path.
#
# No vendor ships an arm64 desktop Linux build with H.264 baked in --
# checked and ruled out Microsoft Edge, Brave, Vivaldi, and the xtradeb PPA
# (none publish arm64 packages/binaries at all, confirmed via each vendor's
# own apt/PPA repo metadata). Mozilla is the one still-maintained vendor
# that ships an official arm64 Linux build, and Firefox bundles Cisco's
# OpenH264 plugin specifically for WebRTC's H.264 (Cisco pays the patent
# license for exactly this use case) -- verified via getCapabilities and a
# real screenshot of the /3d page rendering all three camera panels
# through actual WebRTC decode, no black frames, no flicker.
#
# Pinned to a specific release tarball (not the "latest" redirect) so the
# image is reproducible across builds; bump deliberately, re-verify H.264
# capability after bumping (Mozilla could in principle drop OpenH264).
ARG FIREFOX_VERSION=152.0.5
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgtk-3-0 \
    libx11-xcb1 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/* \
    && wget -q "https://download-installer.cdn.mozilla.net/pub/firefox/releases/${FIREFOX_VERSION}/linux-aarch64/en-US/firefox-${FIREFOX_VERSION}.tar.xz" -O /tmp/firefox.tar.xz \
    && tar xf /tmp/firefox.tar.xz -C /opt \
    && rm /tmp/firefox.tar.xz

# Kiosk profile (see scripts/firefox-kiosk-user.js for what/why). Root's
# real profile dir is used (not /tmp) so it survives container restarts.
COPY scripts/firefox-kiosk-user.js /opt/firefox-kiosk-profile/user.js

# Non-root account to actually run the kiosk Firefox process (see the `su`
# in scripts/open_web_3d_right.sh). Firefox refuses to enable its content
# sandbox for uid 0 and instead shows a permanent "security sandbox is
# disabled, unsupported and less secure" bar -- Mozilla hardcodes this
# warning with no pref/policy to suppress it (sandboxing root is
# meaningless, so they deliberately don't make it hideable). uid 1000
# mirrors this host's desktop user (nvidia) on purpose: the X server
# authorizes by the *kernel* uid of the connecting process (this Docker
# setup has no userns-remap, so container uid 1000 IS host uid 1000), and
# scripts/run_dashboard.sh's `xhost +SI:localuser:$(id -un)` grant is keyed
# off that same uid -- the container-local username below doesn't need to
# match anything. GID 104 is "render" on the host (/dev/dri/renderD128)
# but collides with systemd-resolve's GID inside this image; joining it by
# number still grants the device access that group membership is for.
RUN useradd -m -u 1000 -G video,104 -s /bin/bash kiosk \
    && chown -R kiosk:kiosk /opt/firefox-kiosk-profile

# ── GStreamer core + PyGObject for the NVJPEG hardware JPEG path ────────────
# scripts/hw_jpeg.py drives nvjpegenc/nvjpegdec through GStreamer. The NVIDIA
# plugin .so files themselves (libgstnvjpeg.so, libgstnvvidconv.so, ...) are
# NOT baked in -- the nvidia container runtime injects them from the host per
# /etc/nvidia-container-runtime/host-files-for-container.d/drivers.csv -- but
# they need the GStreamer framework and Python bindings present to load into.
# gstreamer1.0-tools also provides gst-inspect-1.0, which the Settings page's
# /api/images/capabilities probe shells out to. Kept as its own layer (not in
# the apt layer at the top) so the cached playwright/pip layers don't rebuild.
# plugins-bad (webrtcbin/dtls/srtp/h264parse) + nice (ICE) serve the WebRTC
# camera streams in scripts/webrtc_stream.py; the H.264 encoder itself is the
# host-injected nvv4l2h264enc. sqlite3 (the CLI) is the .recover salvage tool
# for power-cut-corrupted recordings (post_processing.py orphan recovery).
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-nice \
    python3-gi \
    gir1.2-gst-plugins-base-1.0 \
    gir1.2-gst-plugins-bad-1.0 \
    && rm -rf /var/lib/apt/lists/*

# ── Interactive shells: source ROS2 for plain `docker exec -it ... bash` ────
# docker_entrypoint.sh only wraps the container's own CMD; a `docker exec`
# shell attaches directly to bash and skips it, so `ros2 ...` fails with
# import errors (PATH has the binary, but PYTHONPATH/AMENT_PREFIX_PATH from
# setup.bash are missing). Mirrors devcontainer.json's postCreateCommand,
# which only fixes this for VS Code's own terminals, not a bare exec.
RUN echo 'source /opt/ros/humble/setup.bash' >> /root/.bashrc \
    && echo 'export LD_LIBRARY_PATH=/lib:/lib/aarch64-linux-gnu:/usr/local/cuda/lib64:$LD_LIBRARY_PATH' >> /root/.bashrc

# ── Entrypoint: sources ROS2 and sets library paths ─────────────────────────
COPY scripts/docker_entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# ── Application code (baked in for image-tarball releases) ──────────────────
# Customer deployments (deploy/docker-compose.yml) run this baked copy and
# mount only the data dirs (config/, rosbags/, outputs/, runs/) from the host.
# The dev compose file at the repo root still live-mounts the whole repo over
# this path, shadowing it, so dev iteration is unaffected. Kept as the last
# layer: code-only changes rebuild in seconds, everything above stays cached.
# .dockerignore keeps runtime data, .git, and release/ tarballs out.
COPY . /workspaces/insight_capture

# ── Working directory matches docker-compose source mount path
WORKDIR /workspaces/insight_capture

EXPOSE 8765

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "-u", "scripts/multi_camera_dashboard_web.py"]
