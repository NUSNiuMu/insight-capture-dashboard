cmake_minimum_required(VERSION 3.16)
project(insight_native_mapping LANGUAGES C CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
find_package(Eigen3 REQUIRED)
find_package(OpenCV 4 REQUIRED)
find_package(glog REQUIRED)
find_package(gflags REQUIRED)
find_package(nlohmann_json REQUIRED)
find_package(Ceres 2.1 REQUIRED PATHS /opt/looper-deps NO_DEFAULT_PATH)
set(UPSTREAM ${CMAKE_CURRENT_SOURCE_DIR}/third_party/loopervio)
set(DBOW ${CMAKE_CURRENT_SOURCE_DIR}/third_party/dbow3/src)
file(MAKE_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/include/DBoW3)
file(GLOB DBOW_HEADERS ${DBOW}/*.h)
foreach(header ${DBOW_HEADERS})
  get_filename_component(name ${header} NAME)
  configure_file(${header} ${CMAKE_CURRENT_BINARY_DIR}/include/DBoW3/${name} COPYONLY)
endforeach()
add_library(dbow3 STATIC ${DBOW}/BowVector.cpp ${DBOW}/Database.cpp ${DBOW}/DescManip.cpp
  ${DBOW}/FeatureVector.cpp ${DBOW}/QueryResults.cpp ${DBOW}/ScoringObject.cpp
  ${DBOW}/Vocabulary.cpp ${DBOW}/quicklz.c)
target_include_directories(dbow3 PUBLIC ${DBOW} ${CMAKE_CURRENT_BINARY_DIR}/include ${OpenCV_INCLUDE_DIRS})
target_compile_definitions(dbow3 PUBLIC OPENCV_VERSION_3)
target_compile_options(dbow3 PRIVATE "$<$<COMPILE_LANGUAGE:CXX>:-include;cstdint>")
target_link_libraries(dbow3 PUBLIC ${OpenCV_LIBS})
set(CORE
  common/timer.cc common/image_undistortion.cc common/equidistant_distortion.cc
  common/radial_tangential_distortion.cc config/sensor_calibration.cc
  math/matrix_base.cc math/transformation_utils.cc math/pnp.cc math/polynomial.cc
  ransac/random_sampler.cc ransac/random.cc ransac/support_measurement.cc
  vio/data_types.cc vio/pose_local_parameterization.cc vio/backend/imu_preintegration.cc
  vio/frontend/orb_extractor.cc vio/triangulation/triangulation.cc vio/thread/vio_thread.cc
  vio/cost_functions/projection_factor.cc vio/cost_functions/relative_pose_factor.cc
  vio/map/key_frame.cc vio/map/map_point.cc vio/map/map.cc vio/map/orb_matcher.cc
  vio/map/ba_optimizer.cc vio/map/local_mapping.cc vio/map/loop_closure.cc vio/map/pose_sever.cc)
list(TRANSFORM CORE PREPEND ${UPSTREAM}/)
add_library(looper_core STATIC ${CORE})
target_include_directories(looper_core PUBLIC ${UPSTREAM} ${UPSTREAM}/vio
  ${CMAKE_CURRENT_SOURCE_DIR}/third_party/sophus ${OpenCV_INCLUDE_DIRS})
target_compile_definitions(looper_core PUBLIC CMAKE_BUILD DISABLE_VIO_MCAP DISABLE_RELOCALIZATION SOPHUS_USE_BASIC_LOGGING)
target_compile_options(looper_core PRIVATE -ffunction-sections -fdata-sections)
target_link_libraries(looper_core PUBLIC Eigen3::Eigen Ceres::ceres glog::glog gflags
  nlohmann_json::nlohmann_json dbow3 ${OpenCV_LIBS} pthread)
add_executable(insight_mapping src/server.cc src/engine.cc)
target_link_libraries(insight_mapping PRIVATE looper_core)
target_link_options(insight_mapping PRIVATE -Wl,--gc-sections)
install(TARGETS insight_mapping RUNTIME DESTINATION bin)
add_executable(check_pose_graph EXCLUDE_FROM_ALL tests/check_pose_graph.cc)
target_link_libraries(check_pose_graph PRIVATE looper_core)
target_link_options(check_pose_graph PRIVATE -Wl,--gc-sections)
