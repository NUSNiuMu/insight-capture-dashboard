// Copyright 2020 DeepMirror Inc. All rights reserved.

#ifndef MATH_TRANSFORMATION_UTILS_H_
#define MATH_TRANSFORMATION_UTILS_H_

#include <limits>
#include "Eigen/Dense"
#include "sophus/so3.hpp"

namespace dm {
namespace math {

template<typename Derived>
Eigen::Quaternion<typename Derived::Scalar> AngleVectorToRotation(
    const Eigen::MatrixBase<Derived>& angle_vector) {
  using T = typename Derived::Scalar;
  const T angle = angle_vector.norm();
  return angle < std::numeric_limits<T>::epsilon()
         ? Eigen::Quaternion<T>::Identity()
         : Eigen::Quaternion<T>(Eigen::AngleAxis<T>(angle, angle_vector.normalized()));
}

template<typename Derived>
Eigen::Matrix<typename Derived::Scalar, 3, 1> AngleVectorFromRotation(
    const Eigen::MatrixBase<Derived>& rotation) {
  using T = typename Derived::Scalar;
  Eigen::AngleAxis<T> angleaxis(rotation);
  return angleaxis.axis() * angleaxis.angle();
}

template<typename Derived>
Eigen::Vector3d AngleVectorFromRotation(const Eigen::QuaternionBase<Derived>& rotation) {
  using T = typename Derived::Scalar;
  Eigen::AngleAxis<T> angleaxis(rotation);
  return angleaxis.axis() * angleaxis.angle();
}
template<typename Derived>
Eigen::Matrix<typename Derived::Scalar, 3, 3> SkewSymmetric(const Eigen::MatrixBase<Derived>& q) {
  using T = typename Derived::Scalar;
  Eigen::Matrix<T, 3, 3> ans;
  ans << T(0), -q(2), q(1), q(2), T(0), -q(0), -q(1), q(0), T(0);
  return ans;
}

template<typename Derived>
Eigen::Quaternion<typename Derived::Scalar> DeltaQ(const Eigen::MatrixBase<Derived>& theta) {
  using T = typename Derived::Scalar;
  Eigen::Quaternion<T> dq;
  dq.w() = T(1.0);
  dq.x() = theta.x() * T(0.5);
  dq.y() = theta.y() * T(0.5);
  dq.z() = theta.z() * T(0.5);
  return dq;
}

template<typename Derived>
Eigen::Matrix<typename Derived::Scalar, 4, 4>
    QuaternionLeft(const Eigen::QuaternionBase<Derived>& q) {
  using T = typename Derived::Scalar;
  Eigen::Matrix<T, 4, 4> ans;
  ans(0, 0) = q.w();
  ans.template block<1, 3>(0, 1) = -q.vec().transpose();
  ans.template block<3, 1>(1, 0) = q.vec();
  ans.template block<3, 3>(1, 1) =
      q.w() * Eigen::Matrix<T, 3, 3>::Identity() + SkewSymmetric(q.vec());
  return ans;
}

template<typename Derived>
Eigen::Matrix<typename Derived::Scalar, 4, 4>
    QuaternionRight(const Eigen::QuaternionBase<Derived>& q) {
  using T = typename Derived::Scalar;
  Eigen::Matrix<T, 4, 4> ans;
  ans(0, 0) = q.w();
  ans.template block<1, 3>(0, 1) = -q.vec().transpose();
  ans.template block<3, 1>(1, 0) = q.vec();
  ans.template block<3, 3>(1, 1) =
      q.w() * Eigen::Matrix<T, 3, 3>::Identity() - SkewSymmetric(q.vec());
  return ans;
}

template<typename Derived>
Eigen::Matrix<typename Derived::Scalar, 3, 3> ExpSO3(const Eigen::MatrixBase<Derived>& w) {
  using T = typename Derived::Scalar;
  return Sophus::SO3<T>::exp(w).matrix();
}

template<typename Derived>
Eigen::Matrix<typename Derived::Scalar, 3, 1> LogSO3(const Eigen::MatrixBase<Derived>& R) {
  using T = typename Derived::Scalar;
  return Sophus::SO3<T>(R).log();
}

template<typename Derived>
Eigen::Vector3d LogAndThetaSO3(const Eigen::MatrixBase<Derived>& R,
                               typename Derived::Scalar* theta) {
  using T = typename Derived::Scalar;
  Eigen::Ref<const Eigen::Matrix<T, 3, 3>> R_ref(R);

  auto log_theta = Sophus::SO3<T>(R_ref).logAndTheta();
  *theta = log_theta.theta;
  return log_theta.tangent;
}

Eigen::Matrix4d ExpSE3(const Eigen::Vector3d& w, const Eigen::Vector3d& t, const double& theta);

/**
 * @brief Integrated quaternion from angular velocity
 * Hamilton local
 * See equation (199) of Joan Solà [Quaternion kinematics for the error-state Kalman filter]
 */
template<typename Derived>
Eigen::Matrix<typename Derived::Scalar, 4, 4> OmegaLocal(const Eigen::MatrixBase<Derived>& w) {
  using T = typename Derived::Scalar;
  Eigen::Matrix<T, 4, 4> mat;
  mat.block(1, 1, 3, 3) = -SkewSymmetric(w);
  mat.block(0, 1, 1, 3) = -w.transpose();
  mat.block(1, 0, 3, 1) = w;
  mat(0, 0) = T(0);
  return mat;
}

/**
 * @brief Integrated quaternion from angular velocity
 * Hamilton global
 * See equation (201) of Joan Solà [Quaternion kinematics for the error-state Kalman filter]
 */
template<typename Derived>
Eigen::Matrix<typename Derived::Scalar, 4, 4> OmegaGlobal(const Eigen::MatrixBase<Derived>& w) {
  using T = typename Derived::Scalar;
  Eigen::Matrix<T, 4, 4> mat;
  mat.block(1, 1, 3, 3) = SkewSymmetric(w);
  mat.block(0, 1, 1, 3) = -w.transpose();
  mat.block(1, 0, 3, 1) = w;
  mat(0, 0) = T(0);
  return mat;
}

Eigen::Vector3d Rotation2YawPitchRoll(const Eigen::Matrix3d& rotation, bool rad = false);
Eigen::Matrix3d YawPitchRoll2Rotation(const Eigen::Vector3d& euler_angle, bool rad = false);
double NormalizeAngle(double angle);

}  // namespace math
}  // namespace dm

#endif  // MATH_TRANSFORMATION_UTILS_H_
