FROM insight-loopervio-deps:2.1 AS build
COPY . /source
RUN cmake -S /source -B /tmp/mapping-build -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/insight-mapping \
    && cmake --build /tmp/mapping-build -j2 \
    && cmake --install /tmp/mapping-build

FROM insight-loopervio-deps:2.1
COPY --from=build /opt/insight-mapping/bin/insight_mapping /usr/local/bin/insight_mapping
RUN mkdir -p /run/looper
CMD ["/usr/local/bin/insight_mapping", "/run/looper/mapping.sock"]
