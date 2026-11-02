FROM insight-loopervio-deps:2.2 AS build
COPY . /source
RUN cmake -S /source -B /tmp/mapping-build -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/insight-mapping \
    && cmake --build /tmp/mapping-build -j2 \
    && cmake --build /tmp/mapping-build --target check_pose_graph -j2 \
    && /tmp/mapping-build/check_pose_graph \
    && cmake --install /tmp/mapping-build

FROM ubuntu:22.04
ARG DEBIAN_FRONTEND=noninteractive
RUN sed -i 's|http://ports.ubuntu.com/ubuntu-ports/|http://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports/|g' /etc/apt/sources.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        libopencv-features2d4.5d libgoogle-glog0v5 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/looper-deps/lib/libceres.so* /opt/looper-deps/lib/
ENV LD_LIBRARY_PATH=/opt/looper-deps/lib
COPY --from=build /opt/insight-mapping/bin/insight_mapping /usr/local/bin/insight_mapping
RUN mkdir -p /run/looper
CMD ["/usr/local/bin/insight_mapping", "/run/looper/mapping.sock"]
