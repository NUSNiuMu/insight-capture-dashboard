// Copyright 2020 DeepMirror Inc. All rights reserved.

#include "math/transformation_utils.h"

#include <limits>

#include "sophus/so3.hpp"

namespace dm {
namespace math {

Eigen::Matrix4d ExpSE3(const Eigen::Vector3d& w, const Eigen::Vector3d& t, const double& theta) {
  Eigen::Matrix4d T = Eigen::Matrix4d::Identity();
  Eigen::Matrix3d w_x = SkewSymmetric(w);

  if (theta < 1e-5) {
    Eigen::Matrix3d e_w_x = Eigen::Matrix3d::Identity();
    T.block(0, 0, 3, 3) = e_w_x;
    T.block(0, 3, 3, 1) = e_w_x * t;
  } else {
    Eigen::Matrix3d e_w_x = Eigen::Matrix3d::Identity() + (std::sin(theta) / theta) * w_x +
        ((1.0 - std::cos(theta)) / (theta * theta)) * w_x * w_x;

    Eigen::Matrix3d V = Eigen::Matrix3d::Identity() +
        ((1.0 - std::cos(theta)) / (theta * theta)) * w_x +
        ((theta - std::sin(theta)) / (theta * theta * theta)) * w_x * w_x;

    Eigen::Vector3d _t = V * t;

    T.block(0, 0, 3, 3) = e_w_x;
    T.block(0, 3, 3, 1) = _t;
  }
  return T;
}



Eigen::Vector3d Rotation2YawPitchRoll(const Eigen::Matrix3d& rotation, bool rad) {
  Eigen::Vector3d ypr;
  ypr(0) = atan2(rotation.col(0)(1), rotation.col(0)(0));
  ypr(1) = atan2(-rotation.col(0)(2),
                 rotation.col(0)(0) * cos(ypr[0]) + rotation.col(0)(1) * sin(ypr[0]));
  ypr(2) = atan2(rotation.col(2)(0) * sin(ypr[0]) - rotation.col(2)(1) * cos(ypr[0]),
                 -rotation.col(1)(0) * sin(ypr[0]) + rotation.col(1)(1) * cos(ypr[0]));
  if (rad) {
    return ypr;
  } else {
    return ypr / M_PI * 180.0;
  }
}

Eigen::Matrix3d YawPitchRoll2Rotation(const Eigen::Vector3d& euler_angle,  bool rad) {
  const double y = rad ? euler_angle.x() : euler_angle.x() * M_PI / 180.0;
  const double p = rad ? euler_angle.y() : euler_angle.y() * M_PI / 180.0;
  const double r = rad ? euler_angle.z() : euler_angle.z() * M_PI / 180.0;
  Eigen::Matrix3d Rz;
  Rz << cos(y), -sin(y), 0, sin(y), cos(y), 0, 0, 0, 1;
  Eigen::Matrix3d Ry;
  Ry << cos(p), 0., sin(p), 0., 1., 0., -sin(p), 0., cos(p);
  Eigen::Matrix3d Rx;
  Rx << 1., 0., 0., 0., cos(r), -sin(r), 0., sin(r), cos(r);
  return Rz * Ry * Rx;
}

double NormalizeAngle(double angle) {
  double two_pi(2.0 * 180);
  if (angle > 0)
    return angle - two_pi * std::floor((angle + 180.0) / two_pi);
  else
    return angle + two_pi * std::floor((-angle + 180.0) / two_pi);
}

}  // namespace math
}  // namespace dm
