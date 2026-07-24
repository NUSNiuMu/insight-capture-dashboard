#!/usr/bin/env bash

set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "Usage: $0 <webkit-source> <build-dir> [install-prefix]" >&2
  exit 2
fi

SOURCE_DIR="$(realpath "$1")"
BUILD_DIR="$2"
INSTALL_PREFIX="${3:-/opt/webkitgtk-webrtc}"
CC_BIN="${CC:-$(command -v gcc-12 || command -v gcc)}"
CXX_BIN="${CXX:-$(command -v g++-12 || command -v g++)}"

if [[ ! -f "${SOURCE_DIR}/Source/cmake/OptionsGTK.cmake" ]]; then
  echo "Not a WebKitGTK source tree: ${SOURCE_DIR}" >&2
  exit 2
fi

mkdir -p "${BUILD_DIR}"

cmake \
  -S "${SOURCE_DIR}" \
  -B "${BUILD_DIR}" \
  -GNinja \
  -DPORT=GTK \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER="${CC_BIN}" \
  -DCMAKE_CXX_COMPILER="${CXX_BIN}" \
  -DCMAKE_INSTALL_PREFIX="${INSTALL_PREFIX}" \
  -DENABLE_WEB_RTC=ON \
  -DUSE_GSTREAMER_WEBRTC=ON \
  -DUSE_GSTREAMER_GL=ON \
  -DENABLE_MINIBROWSER=ON \
  -DENABLE_DOCUMENTATION=OFF \
  -DENABLE_INTROSPECTION=OFF \
  -DENABLE_BUBBLEWRAP_SANDBOX=OFF \
  -DDEBUG_FISSION=OFF \
  -DENABLE_GAMEPAD=OFF \
  -DENABLE_SPEECH_SYNTHESIS=OFF \
  -DENABLE_WEBDRIVER=OFF \
  -DENABLE_WEBXR=OFF \
  -DUSE_GTK4=OFF \
  -DUSE_SOUP2=OFF \
  -DUSE_AVIF=OFF \
  -DUSE_JPEGXL=OFF \
  -DUSE_LIBBACKTRACE=OFF \
  -DUSE_OPENJPEG=OFF \
  -DWTF_CPU_ARM64_CORTEXA53=OFF

grep -E \
  '^(ENABLE_WEB_RTC|USE_GSTREAMER_WEBRTC|USE_GSTREAMER_GL|ENABLE_MINIBROWSER):' \
  "${BUILD_DIR}/CMakeCache.txt"
