#include "vio/pose_local_parameterization.h"
#include "math/transformation_utils.h"

namespace dm {
namespace vio {

bool PoseManifold::Plus(const double* x, const double* delta, double* x_plus_delta) const {
  Eigen::Map<const Eigen::Vector3d> rotv(x);
  // Eigen::Quaterniond dq = dm::math::AngleVectorToRotation(Eigen::Vector3d(delta[0], delta[1],
  // delta[2]));
  Eigen::Quaterniond dq = Eigen::Quaterniond(1.0, delta[0] * 0.5, delta[1] * 0.5, delta[2] * 0.5);
  // LOG(INFO) << "dq " << dq.coeffs().transpose();
  Eigen::Vector3d rotv_plus_delta =
      dm::math::AngleVectorFromRotation((dm::math::AngleVectorToRotation(rotv) * dq).normalized());
  x_plus_delta[0] = rotv_plus_delta[0];
  x_plus_delta[1] = rotv_plus_delta[1];
  x_plus_delta[2] = rotv_plus_delta[2];
  x_plus_delta[3] = x[3] + delta[3];
  x_plus_delta[4] = x[4] + delta[4];
  x_plus_delta[5] = x[5] + delta[5];
  return true;
}

bool PoseManifold::PlusJacobian(const double* x, double* jacobian) const {
  Eigen::Map<Eigen::Matrix<double, 6, 6, Eigen::RowMajor>> j(jacobian);
  j.setIdentity();
  return true;
}

bool GravityManifold::Plus(const double* x, const double* delta, double* x_plus_delta) const {
  Eigen::Quaterniond dq = Eigen::Quaterniond(1.0, delta[0] * 0.5, delta[1] * 0.5, delta[2] * 0.5);
  dq.normalize();
  Eigen::Map<const Eigen::Vector3d> gravity(x);
  Eigen::Map<Eigen::Vector3d> update_gravity(x_plus_delta);
  update_gravity = dq * gravity;
  return true;
}

bool GravityManifold::PlusJacobian(const double* x, double* jacobian) const {
  Eigen::Map<Eigen::Matrix<double, 3, 3, Eigen::RowMajor>> j(jacobian);
  Eigen::Map<const Eigen::Vector3d> gravity(x);
  j = -dm::math::SkewSymmetric(gravity);
  return true;
}

bool YawPoseManifold::Plus(const double* x, const double* delta, double* x_plus_delta) const {
  Eigen::Map<const Eigen::Vector3d> rotv(x);
  Eigen::Quaterniond dq = Eigen::Quaterniond(1.0, 0.0, 0.0, 0.5 * delta[0]);
  dq.normalize();
  Eigen::Vector3d rotv_plus_delta =
      dm::math::AngleVectorFromRotation((dq * dm::math::AngleVectorToRotation(rotv)).normalized());
  x_plus_delta[0] = rotv_plus_delta[0];
  x_plus_delta[1] = rotv_plus_delta[1];
  x_plus_delta[2] = rotv_plus_delta[2];
  x_plus_delta[3] = x[3] + delta[1];
  x_plus_delta[4] = x[4] + delta[2];
  x_plus_delta[5] = x[5] + delta[3];
  return true;
}

bool YawPoseManifold::PlusJacobian(const double* x, double* jacobian) const {
  Eigen::Map<Eigen::Matrix<double, 6, 4, Eigen::RowMajor>> j(jacobian);
  j.setZero();
  j(2, 0) = 1;
  j.block<3, 3>(3, 1).setIdentity();
  return true;
}

}  // namespace vio
}  // namespace dm
