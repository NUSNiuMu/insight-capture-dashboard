add_library(image_undistortion SHARED
    distortion.h
    equidistant_distortion.cc
    equidistant_distortion.h
    image_undistortion.cc
    image_undistortion.h
    radial_tangential_distortion.cc
    radial_tangential_distortion.h
)

target_compile_definitions(image_undistortion PRIVATE CMAKE_BUILD DISABLE_VIO_MCAP DISABLE_RELOCALIZATION)

target_link_libraries(image_undistortion
    PRIVATE
        sensor_calibration
        Sophus::Sophus
        Eigen3::Eigen
        ${OpenCV_LIBS}
        glog::glog
        gflags
        nlohmann_json::nlohmann_json
)

target_include_directories(image_undistortion PRIVATE
    ${CMAKE_SOURCE_DIR}
)