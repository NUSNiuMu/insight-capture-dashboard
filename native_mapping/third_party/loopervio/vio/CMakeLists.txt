set(TIMER
    ${CMAKE_CURRENT_SOURCE_DIR}/../common/timer.cc
)

set(MATH
    ${CMAKE_CURRENT_SOURCE_DIR}/../math/matrix_base.cc
    ${CMAKE_CURRENT_SOURCE_DIR}/../math/pnp.cc
    ${CMAKE_CURRENT_SOURCE_DIR}/../math/polynomial.cc
    ${CMAKE_CURRENT_SOURCE_DIR}/../math/transformation_utils.cc
)

set(RANSAC
    ${CMAKE_CURRENT_SOURCE_DIR}/../ransac/random_sampler.cc
    ${CMAKE_CURRENT_SOURCE_DIR}/../ransac/random.cc
    ${CMAKE_CURRENT_SOURCE_DIR}/../ransac/support_measurement.cc
)

# This repo contains only the compiled VIO closure carved out of Sigma. The
# wasm / pseudo-frontend / relocalizer / pnp_ransac / plane_recon / data_io
# sources and *_test.cc were intentionally left out at extraction time (they are
# excluded upstream via DISABLE_RELOCALIZATION and Bazel-only dependencies), so
# the recursive glob picks up exactly the sources that make up libnaive_vio.
# The two filters below remain as defensive guards.
file(GLOB_RECURSE SOURCES "${CMAKE_CURRENT_SOURCE_DIR}/*.cc")
list(FILTER SOURCES EXCLUDE REGEX ".*_test\\.cc$")
list(FILTER SOURCES EXCLUDE REGEX "${CMAKE_CURRENT_SOURCE_DIR}/data_io/.*")

add_library(naive_vio SHARED ${SOURCES} ${HEADERS} ${TIMER} ${MATH} ${RANSAC})

target_compile_definitions(naive_vio PRIVATE CMAKE_BUILD DISABLE_VIO_MCAP DISABLE_RELOCALIZATION)

# PC-debug build: enable the front-end's built-in optical-flow imshow windows.
#  - FRONTEND_DEBUG_MODE gates the main "Track feat." optical-flow window, which
#    supports frame stepping ('p' toggles single-step, 's'/'w' slow/speed the
#    feed) — this is the primary debug/step window.
#  - FAST_OF_ENABLE_* gate the extra stereo-tracking / tracking_final overlays.
if(SIGMAVIO_BUILD_DEBUG)
  target_compile_definitions(naive_vio PRIVATE
      FRONTEND_DEBUG_MODE=1
      FAST_OF_ENABLE_DEBUG_VISUALIZATION
      FAST_OF_ENABLE_STEREO_TRACKING_VISUALIZATION)
endif()

target_link_libraries(naive_vio
    PRIVATE
        vio_config
        image_undistortion
        Sophus::Sophus
        Eigen3::Eigen
        ${OpenCV_LIBS}
        DBoW3
        glog::glog
        gflags
        Ceres::ceres
        nlohmann_json::nlohmann_json
)

target_include_directories(naive_vio
    PUBLIC
        ${CMAKE_CURRENT_SOURCE_DIR}/../
)
