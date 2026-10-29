add_library(sensor_calibration SHARED
    sensor_calibration.cc
    sensor_calibration.h
)

target_compile_definitions(sensor_calibration PRIVATE CMAKE_BUILD DISABLE_VIO_MCAP DISABLE_RELOCALIZATION)

target_link_libraries(sensor_calibration
    PRIVATE
        Eigen3::Eigen
        glog::glog
        nlohmann_json::nlohmann_json
)

target_include_directories(sensor_calibration PRIVATE
    ${CMAKE_SOURCE_DIR}
)

add_library(vio_config SHARED
    vio_config.cc
    vio_config.h
)

target_compile_definitions(vio_config PRIVATE CMAKE_BUILD DISABLE_VIO_MCAP DISABLE_RELOCALIZATION)

target_link_libraries(vio_config
    PRIVATE
        sensor_calibration
        Eigen3::Eigen
        ${OpenCV_LIBS}
        Sophus::Sophus
        glog::glog
)

target_include_directories(vio_config PRIVATE
    ${CMAKE_SOURCE_DIR}
)