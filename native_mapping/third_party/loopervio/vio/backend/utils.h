#ifndef VIO_BACKEND_UTILS_H_
#define VIO_BACKEND_UTILS_H_

#include "Eigen/Core"
#include "Eigen/Geometry"

namespace dm::vio {

// p_0: initial position
// q_0: initial orientation
// v_0: initial velocity
// accel_0: initial acceleration
// gyro_0: initial angular velocity
// dt: time step
// accel_1: next acceleration
// gyro_1: next angular velocity
// gravity: defined as rotation_self_to_world * acc_self + gravity = acc_in_world
void RK4ImuIntegration(const Eigen::Vector3d& p_0, const Eigen::Quaterniond& q_0,
                       const Eigen::Vector3d& v_0, const Eigen::Vector3d& accel_0,
                       const Eigen::Vector3d& gyro_0, const double& dt,
                       const Eigen::Vector3d& accel_1, const Eigen::Vector3d& gyro_1,
                       const Eigen::Vector3d& gravity, Eigen::Vector3d* output_p,
                       Eigen::Quaterniond* output_q, Eigen::Vector3d* output_v);

}  // namespace dm::vio

#endif  // VIO_BACKEND_UTILS_H_
