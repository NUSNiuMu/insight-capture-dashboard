// Copyright 2020 DeepMirror Inc. All rights reserved.

#ifndef VIO_BACKEND_FRAME_STATE_H_
#define VIO_BACKEND_FRAME_STATE_H_

#include <Eigen/Dense>

namespace dm {
namespace vio {

struct FrameState {
  FrameState()
      : position(0.0, 0.0, 0.0),
        orientation(Eigen::Quaterniond::Identity()),
        velocity(0.0, 0.0, 0.0),
        acceleration_bias(0.0, 0.0, 0.0),
        gyroscope_bias(0.0, 0.0, 0.0),
        gravity(0.0, 0.0, 0.0),
        timestamp(0),
        confidence(0.0) {}
  Eigen::Vector3d position;
  Eigen::Quaterniond orientation;
  Eigen::Vector3d velocity;
  Eigen::Vector3d acceleration_bias;
  Eigen::Vector3d gyroscope_bias;
  Eigen::Vector3d gravity;
  uint64_t timestamp;
  double confidence;

  EIGEN_MAKE_ALIGNED_OPERATOR_NEW
};

}  // namespace vio
}  // namespace dm

#endif  // VIO_BACKEND_FRAME_STATE_H_
