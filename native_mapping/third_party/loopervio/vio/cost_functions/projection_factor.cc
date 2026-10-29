// Copyright 2021 DeepMirror Inc. All rights reserved.

#include "vio/cost_functions/projection_factor.h"

namespace dm {
namespace vio {

template <typename T>
bool CameraProjectionError::operator()(const T* const imu_pose, const T* const point,
                                       T* residuals) const {
  T imu_rotation_array[4];
  ceres::AngleAxisToQuaternion(imu_pose, imu_rotation_array);
  Eigen::Quaternion<T> imu_rotation(imu_rotation_array[0], imu_rotation_array[1],
                                    imu_rotation_array[2], imu_rotation_array[3]);
  Eigen::Matrix<T, 3, 1> point_eigen(point[0], point[1], point[2]);
  Eigen::Matrix<T, 3, 1> imu_translation(imu_pose[3], imu_pose[4], imu_pose[5]);
  Eigen::Quaternion<T> camera_extrinsics_rotation(
      T(camera_extrinsics_.rotation().w()), T(camera_extrinsics_.rotation().x()),
      T(camera_extrinsics_.rotation().y()), T(camera_extrinsics_.rotation().z()));
  Eigen::Matrix<T, 3, 1> camera_extrinsics_translation(T(camera_extrinsics_.translation().x()),
                                                       T(camera_extrinsics_.translation().y()),
                                                       T(camera_extrinsics_.translation().z()));
  Eigen::Quaternion<T> camera_rotation = imu_rotation * camera_extrinsics_rotation;
  Eigen::Matrix<T, 3, 1> camera_translation =
      imu_rotation * camera_extrinsics_translation + imu_translation;
  Eigen::Matrix<T, 3, 1> local_point =
      camera_rotation.inverse() * (point_eigen - camera_translation);

  residuals[0] = local_point.x() / local_point.z() - T(observation_.x());
  residuals[1] = local_point.y() / local_point.z() - T(observation_.y());
  residuals[0] *= T(weight_.x());
  residuals[1] *= T(weight_.y());

  return true;
}

ceres::CostFunction* CameraProjectionError::CreateCostFunction(
    const math::Rigid3d& camera_extrinsics, const Eigen::Vector2d& observation,
    const Eigen::Vector2d& weight) {
  return new ceres::AutoDiffCostFunction<CameraProjectionError, 2, 6, 3>(
      new CameraProjectionError(camera_extrinsics, observation, weight));
}

CameraProjectionFactor::CameraProjectionFactor(const math::Rigid3d &camera_extrinsics,
                                               const Eigen::Vector2d &observation,
                                               const Eigen::Vector2d &weight)
    : camera_extrinsics_(camera_extrinsics), observation_(observation) {
  sqrt_info_ << weight[0], 0, 0, weight[1];
}

CameraProjectionFactor::CameraProjectionFactor(const math::Rigid3d& camera_extrinsics,
                                               const Eigen::Vector2d& observation,
                                               const Eigen::Matrix2d& sqrt_info)
    : camera_extrinsics_(camera_extrinsics), observation_(observation), sqrt_info_(sqrt_info) {}

bool CameraProjectionFactor::Evaluate(double const* const* parameters, double* residuals,
                                      double** jacobians) const {
  Eigen::Quaterniond qwi =
      dm::math::AngleVectorToRotation(Eigen::Map<const Eigen::Vector3d>(parameters[0]));
  Eigen::Map<const Eigen::Vector3d> wti(parameters[0] + 3);
  Eigen::Map<const Eigen::Vector3d> wP(parameters[1]);
  Eigen::Vector3d iP = qwi.inverse() * (wP - wti);
  Eigen::Vector3d cP = camera_extrinsics_.Inverse() * iP;

  Eigen::Map<Eigen::Vector2d> residual(residuals);
  residual = sqrt_info_ *
             Eigen::Vector2d(cP[0] / cP[2] - observation_[0], cP[1] / cP[2] - observation_[1]);

  if (jacobians) {
    Eigen::Matrix3d Rwi = qwi.toRotationMatrix();
    Eigen::Matrix3d Ric = camera_extrinsics_.rotation().toRotationMatrix();

    Eigen::Matrix<double, 2, 3> J_r_cP;
    J_r_cP << 1.0 / cP[2], 0, -cP[0] / (cP[2] * cP[2]), 0, 1.0 / cP[2], -cP[1] / (cP[2] * cP[2]);
    Eigen::Matrix<double, 3, 3> J_cP_iP = Ric.inverse();

    if (jacobians[0]) {
      Eigen::Matrix<double, 3, 6> J_iP_Twi;
      J_iP_Twi.block<3, 3>(0, 0) = dm::math::SkewSymmetric(iP);
      J_iP_Twi.block<3, 3>(0, 3) = -Rwi.inverse();
      Eigen::Map<Eigen::Matrix<double, 2, 6, Eigen::RowMajor>> jacobian_pose(jacobians[0]);
      jacobian_pose = sqrt_info_ * J_r_cP * J_cP_iP * J_iP_Twi;
    }

    if (jacobians[1]) {
      Eigen::Matrix<double, 3, 3> J_iP_wP = Rwi.inverse();
      Eigen::Map<Eigen::Matrix<double, 2, 3, Eigen::RowMajor>> jacobian_point(jacobians[1]);
      jacobian_point = sqrt_info_ * J_r_cP * J_cP_iP * J_iP_wP;
    }
  }
  return true;
}

Eigen::Vector2d CameraProjectionFactor::CalcResidual(double const* const* parameters) {
  Eigen::Quaterniond qwi =
      dm::math::AngleVectorToRotation(Eigen::Map<const Eigen::Vector3d>(parameters[0]));
  Eigen::Map<const Eigen::Vector3d> wti(parameters[0] + 3);
  Eigen::Map<const Eigen::Vector3d> wP(parameters[1]);
  Eigen::Vector3d iP = qwi.inverse() * (wP - wti);
  Eigen::Vector3d cP = camera_extrinsics_.Inverse() * iP;

  return sqrt_info_ *
         Eigen::Vector2d(cP[0] / cP[2] - observation_[0], cP[1] / cP[2] - observation_[1]);
}

//////////////////////////////////////////
LhsCameraProjectionFactor::LhsCameraProjectionFactor(const math::Rigid3d& camera_extrinsics,
                                                     const Eigen::Vector2d& observation,
                                                     const Eigen::Vector2d& weight)
    : camera_extrinsics_(camera_extrinsics), observation_(observation) {
  sqrt_info_ << weight[0], 0, 0, weight[1];
}

LhsCameraProjectionFactor::LhsCameraProjectionFactor(const math::Rigid3d& camera_extrinsics,
                                                     const Eigen::Vector2d& observation,
                                                     const Eigen::Matrix2d& sqrt_info)
    : camera_extrinsics_(camera_extrinsics), observation_(observation), sqrt_info_(sqrt_info) {}

bool LhsCameraProjectionFactor::Evaluate(double const* const* parameters, double* residuals,
                                      double** jacobians) const {
  Eigen::Quaterniond qwi =
      dm::math::AngleVectorToRotation(Eigen::Map<const Eigen::Vector3d>(parameters[0]));
  Eigen::Map<const Eigen::Vector3d> wti(parameters[0] + 3);
  Eigen::Map<const Eigen::Vector3d> wP(parameters[1]);
  Eigen::Vector3d iP = qwi.inverse() * (wP - wti);
  Eigen::Vector3d cP = camera_extrinsics_.Inverse() * iP;

  Eigen::Map<Eigen::Vector2d> residual(residuals);
  residual = sqrt_info_ *
             Eigen::Vector2d(cP[0] / cP[2] - observation_[0], cP[1] / cP[2] - observation_[1]);

  if (jacobians) {
    Eigen::Matrix3d Rwi = qwi.toRotationMatrix();
    Eigen::Matrix3d Ric = camera_extrinsics_.rotation().toRotationMatrix();

    Eigen::Matrix<double, 2, 3> J_r_cP;
    J_r_cP << 1.0 / cP[2], 0, -cP[0] / (cP[2] * cP[2]), 0, 1.0 / cP[2], -cP[1] / (cP[2] * cP[2]);
    Eigen::Matrix<double, 3, 3> J_cP_iP = Ric.inverse();

    if (jacobians[0]) {
      Eigen::Matrix<double, 3, 6> J_iP_Twi;
      J_iP_Twi.block<3, 3>(0, 0) = Rwi.inverse() * dm::math::SkewSymmetric(wP-wti);
      J_iP_Twi.block<3, 3>(0, 3) = -Rwi.inverse();
      Eigen::Map<Eigen::Matrix<double, 2, 6, Eigen::RowMajor>> jacobian_pose(jacobians[0]);
      jacobian_pose = sqrt_info_ * J_r_cP * J_cP_iP * J_iP_Twi;
    }

    if (jacobians[1]) {
      Eigen::Matrix<double, 3, 3> J_iP_wP = Rwi.inverse();
      Eigen::Map<Eigen::Matrix<double, 2, 3, Eigen::RowMajor>> jacobian_point(jacobians[1]);
      jacobian_point = sqrt_info_ * J_r_cP * J_cP_iP * J_iP_wP;
    }
  }
  return true;
}

bool RollingCameraProjectionFactor::Evaluate(double const* const* parameters,
                                                     double* residuals, double** jacobians) const {
  Eigen::Quaterniond qwi =
      dm::math::AngleVectorToRotation(Eigen::Map<const Eigen::Vector3d>(parameters[0]));
  Eigen::Map<const Eigen::Vector3d> wti(parameters[0] + 3);
  Eigen::Map<const Eigen::Vector3d> wP(parameters[1]);
  Eigen::Map<const Eigen::Vector3d> velocity(parameters[2]);


  Eigen::Vector3d iP = qwi.inverse() * (wP - wti - velocity * rolling_dt_);
  Eigen::Vector3d riP = rolling_inc_q_.inverse() * iP;

  Eigen::Vector3d cP = camera_extrinsic_.Inverse() * riP;
  Eigen::Map<Eigen::Vector2d> residual(residuals);
  residual = sqrt_info_ *
             Eigen::Vector2d(cP[0] / cP[2] - observation_[0], cP[1] / cP[2] - observation_[1]);

  if (jacobians) {
    Eigen::Matrix3d Ric = camera_extrinsic_.rotation().toRotationMatrix();

    Eigen::Matrix<double, 2, 3> J_r_cP;
    J_r_cP << 1.0 / cP[2], 0, -cP[0] / (cP[2] * cP[2]), 0, 1.0 / cP[2], -cP[1] / (cP[2] * cP[2]);
    Eigen::Matrix<double, 3, 3> J_cP_riP = Ric.inverse();

    Eigen::Matrix3d inc_Riw = (qwi * rolling_inc_q_).inverse().matrix();

    if (jacobians[0]) {
      Eigen::Matrix<double, 3, 6> J_riP_Twi;
      J_riP_Twi.block<3, 3>(0, 0) = rolling_inc_q_.inverse().matrix() * dm::math::SkewSymmetric(iP);
      J_riP_Twi.block<3, 3>(0, 3) = -inc_Riw;
      Eigen::Map<Eigen::Matrix<double, 2, 6, Eigen::RowMajor>> jacobian_pose(jacobians[0]);
      jacobian_pose = sqrt_info_ * J_r_cP * J_cP_riP * J_riP_Twi;
    }

    if (jacobians[1]) {
      Eigen::Matrix<double, 3, 3> J_riP_wP = inc_Riw;
      Eigen::Map<Eigen::Matrix<double, 2, 3, Eigen::RowMajor>> jacobian_point(jacobians[1]);
      jacobian_point = sqrt_info_ * J_r_cP * J_cP_riP * J_riP_wP;
    }

    if (jacobians[2]) {
      Eigen::Matrix<double, 3, 3> J_riP_vel = -rolling_dt_ * inc_Riw;
      Eigen::Map<Eigen::Matrix<double, 2, 3, Eigen::RowMajor>> jacobian_velocity(jacobians[2]);
      jacobian_velocity = sqrt_info_ * J_r_cP * J_cP_riP * J_riP_vel;
    }
  }
  return true;
}

Eigen::Vector2d RollingCameraProjectionFactor::CalcResidual(double const* const* parameters) const {
  Eigen::Quaterniond qwi =
      dm::math::AngleVectorToRotation(Eigen::Map<const Eigen::Vector3d>(parameters[0]));
  Eigen::Map<const Eigen::Vector3d> wti(parameters[0] + 3);
  Eigen::Map<const Eigen::Vector3d> wP(parameters[1]);
  Eigen::Map<const Eigen::Vector3d> velocity(parameters[2]);

  Eigen::Vector3d iP = qwi.inverse() * (wP - wti - velocity * rolling_dt_);
  Eigen::Vector3d riP = rolling_inc_q_.inverse() * iP;

  Eigen::Vector3d cP = camera_extrinsic_.Inverse() * riP;
  Eigen::Vector2d residual;
  residual = Eigen::Vector2d(cP[0] / cP[2] - observation_[0], cP[1] / cP[2] - observation_[1]);
  return residual;
}

bool SimpRollingCameraProjectionFactor::Evaluate(double const* const* parameters,
                                             double* residuals, double** jacobians) const {
  Eigen::Quaterniond qwi =
      dm::math::AngleVectorToRotation(Eigen::Map<const Eigen::Vector3d>(parameters[0]));
  Eigen::Map<const Eigen::Vector3d> wti(parameters[0] + 3);
  Eigen::Map<const Eigen::Vector3d> wP(parameters[1]);


  Eigen::Vector3d iP = qwi.inverse() * (wP - wti);
  Eigen::Vector3d riP = rolling_inc_q_.inverse() * iP;

  Eigen::Vector3d cP = camera_extrinsic_.Inverse() * riP;
  Eigen::Map<Eigen::Vector2d> residual(residuals);
  residual = sqrt_info_ *
             Eigen::Vector2d(cP[0] / cP[2] - observation_[0], cP[1] / cP[2] - observation_[1]);

  if (jacobians) {
    Eigen::Matrix3d Ric = camera_extrinsic_.rotation().toRotationMatrix();

    Eigen::Matrix<double, 2, 3> J_r_cP;
    J_r_cP << 1.0 / cP[2], 0, -cP[0] / (cP[2] * cP[2]), 0, 1.0 / cP[2], -cP[1] / (cP[2] * cP[2]);
    Eigen::Matrix<double, 3, 3> J_cP_riP = Ric.inverse();

    Eigen::Matrix3d inc_Riw = (qwi * rolling_inc_q_).inverse().matrix();

    if (jacobians[0]) {
      Eigen::Matrix<double, 3, 6> J_riP_Twi;
      J_riP_Twi.block<3, 3>(0, 0) = rolling_inc_q_.inverse().matrix() * dm::math::SkewSymmetric(iP);
      J_riP_Twi.block<3, 3>(0, 3) = -inc_Riw;
      Eigen::Map<Eigen::Matrix<double, 2, 6, Eigen::RowMajor>> jacobian_pose(jacobians[0]);
      jacobian_pose = sqrt_info_ * J_r_cP * J_cP_riP * J_riP_Twi;
    }

    if (jacobians[1]) {
      Eigen::Matrix<double, 3, 3> J_riP_wP = inc_Riw;
      Eigen::Map<Eigen::Matrix<double, 2, 3, Eigen::RowMajor>> jacobian_point(jacobians[1]);
      jacobian_point = sqrt_info_ * J_r_cP * J_cP_riP * J_riP_wP;
    }
  }
  return true;
}

Eigen::Vector2d SimpRollingCameraProjectionFactor::CalcResidual(
    double const* const* parameters) const {
  Eigen::Quaterniond qwi =
      dm::math::AngleVectorToRotation(Eigen::Map<const Eigen::Vector3d>(parameters[0]));
  Eigen::Map<const Eigen::Vector3d> wti(parameters[0] + 3);
  Eigen::Map<const Eigen::Vector3d> wP(parameters[1]);

  Eigen::Vector3d iP = qwi.inverse() * (wP - wti);
  Eigen::Vector3d riP = rolling_inc_q_.inverse() * iP;

  Eigen::Vector3d cP = camera_extrinsic_.Inverse() * riP;
  Eigen::Vector2d residual;
  residual = Eigen::Vector2d(cP[0] / cP[2] - observation_[0], cP[1] / cP[2] - observation_[1]);
  return residual;
}

template <typename T>
bool ConvexCameraProjectionError::operator()(const T* const imu_pose, const T* const point,
                                             T* residuals) const {
  // imu_pose[0,1,2] are the angle-axis rotation.
  T imu_rotation_array[4];
  ceres::AngleAxisToQuaternion(imu_pose, imu_rotation_array);
  Eigen::Quaternion<T> imu_rotation(imu_rotation_array[0], imu_rotation_array[1],
                                    imu_rotation_array[2], imu_rotation_array[3]);
  Eigen::Matrix<T, 3, 1> point_eigen(point[0], point[1], point[2]);
  Eigen::Matrix<T, 3, 1> imu_translation(imu_pose[3], imu_pose[4], imu_pose[5]);
  Eigen::Quaternion<T> camera_extrinsics_rotation(
      T(camera_extrinsics_.rotation().w()), T(camera_extrinsics_.rotation().x()),
      T(camera_extrinsics_.rotation().y()), T(camera_extrinsics_.rotation().z()));
  Eigen::Matrix<T, 3, 1> camera_extrinsics_translation(T(camera_extrinsics_.translation().x()),
                                                       T(camera_extrinsics_.translation().y()),
                                                       T(camera_extrinsics_.translation().z()));
  Eigen::Quaternion<T> camera_rotation = imu_rotation * camera_extrinsics_rotation;
  Eigen::Matrix<T, 3, 1> camera_translation =
      imu_rotation * camera_extrinsics_translation + imu_translation;
  Eigen::Matrix<T, 3, 1> local_point =
      camera_rotation.inverse() * (point_eigen - camera_translation);

  residuals[0] = local_point.x() - local_point.z() * T(observation_.x());
  residuals[1] = local_point.y() - local_point.z() * T(observation_.y());
  residuals[2] = exp(-local_point.z());
  residuals[0] *= T(weight_.x());
  residuals[1] *= T(weight_.y());
  residuals[2] *= T(weight_.z());

  return true;
}

ceres::CostFunction* ConvexCameraProjectionError::CreateCostFunction(
    const math::Rigid3d& camera_extrinsics, const Eigen::Vector2d& observation,
    const Eigen::Vector3d& weight) {
  return new ceres::AutoDiffCostFunction<ConvexCameraProjectionError, 3, 6, 3>(
      new ConvexCameraProjectionError(camera_extrinsics, observation, weight));
}

ConvexCameraProjectionFactor::ConvexCameraProjectionFactor(const math::Rigid3d& camera_extrinsics,
                                                           const Eigen::Vector2d& observation,
                                                           const Eigen::Vector3d& weight)
    : camera_extrinsics_(camera_extrinsics), observation_(observation) {
  sqrt_info_ << weight[0], 0, 0, 0, weight[1], 0, 0, 0, weight[2];
}

bool ConvexCameraProjectionFactor::Evaluate(double const* const* parameters, double* residuals,
                                            double** jacobians) const {
  Eigen::Quaterniond qwi =
      dm::math::AngleVectorToRotation(Eigen::Map<const Eigen::Vector3d>(parameters[0]));
  Eigen::Map<const Eigen::Vector3d> wti(parameters[0] + 3);
  Eigen::Map<const Eigen::Vector3d> wP(parameters[1]);
  Eigen::Vector3d iP = qwi.inverse() * (wP - wti);
  Eigen::Vector3d cP = camera_extrinsics_.Inverse() * iP;

  Eigen::Map<Eigen::Vector3d> residual(residuals);
  residual(0) = cP[0] - cP[2] * observation_[0];
  residual(1) = cP[1] - cP[2] * observation_[1];
  residual(2) = exp(-cP[2]);
  residual = sqrt_info_ * residual;

  if (jacobians) {
    Eigen::Matrix3d Rwi = qwi.toRotationMatrix();
    Eigen::Matrix3d Ric = camera_extrinsics_.rotation().toRotationMatrix();

    Eigen::Matrix<double, 3, 3> J_r_cP;
    J_r_cP << 1, 0, -observation_[0], 0, 1, -observation_[1], 0, 0, -exp(-cP[2]);
    Eigen::Matrix<double, 3, 3> J_cP_iP = Ric.inverse();

    if (jacobians[0]) {
      Eigen::Matrix<double, 3, 6> J_iP_Twi;
      J_iP_Twi.block<3, 3>(0, 0) = dm::math::SkewSymmetric(iP);
      J_iP_Twi.block<3, 3>(0, 3) = -Rwi.inverse();
      Eigen::Map<Eigen::Matrix<double, 3, 6, Eigen::RowMajor>> jacobian_pose(jacobians[0]);
      jacobian_pose = sqrt_info_ * J_r_cP * J_cP_iP * J_iP_Twi;
    }

    if (jacobians[1]) {
      Eigen::Matrix<double, 3, 3> J_iP_wP = Rwi.inverse();
      Eigen::Map<Eigen::Matrix<double, 3, 3, Eigen::RowMajor>> jacobian_point(jacobians[1]);
      jacobian_point = sqrt_info_ * J_r_cP * J_cP_iP * J_iP_wP;
    }
  }
  return true;
}

Eigen::Vector3d ConvexCameraProjectionFactor::CalcResidual(double const* const* parameters) {
  Eigen::Quaterniond qwi =
      dm::math::AngleVectorToRotation(Eigen::Map<const Eigen::Vector3d>(parameters[0]));
  Eigen::Map<const Eigen::Vector3d> wti(parameters[0] + 3);
  Eigen::Map<const Eigen::Vector3d> wP(parameters[1]);
  Eigen::Vector3d iP = qwi.inverse() * (wP - wti);
  Eigen::Vector3d cP = camera_extrinsics_.Inverse() * iP;

  Eigen::Vector3d residual;
  residual(0) = cP[0] - cP[2] * observation_[0];
  residual(1) = cP[1] - cP[2] * observation_[1];
  residual(2) = exp(-cP[2]);
  return sqrt_info_ * residual;
}

CameraProjectionInverseDepthStereoFactor::CameraProjectionInverseDepthStereoFactor(
    const Eigen::Vector3d& obs_i, const Eigen::Vector3d& obs_j,
    const math::Rigid3d& camera_extrinsics0, const math::Rigid3d& camera_extrinsics1,
    const Eigen::Vector2d& weight)
    : obs_i_(obs_i),
      obs_j_(obs_j),
      camera_extrinsics0_(camera_extrinsics0),
      camera_extrinsics1_(camera_extrinsics1) {
  sqrt_info_ << weight[0], 0, 0, weight[1];
}

bool CameraProjectionInverseDepthStereoFactor::Evaluate(double const* const* parameters,
                                                        double* residuals,
                                                        double** jacobians) const {
  double inv_dep_i = parameters[0][0];

  Eigen::Vector3d pts_camera_i = obs_i_ / inv_dep_i;
  Eigen::Vector3d pts_imu_i = camera_extrinsics0_ * pts_camera_i;
  Eigen::Vector3d pts_camera_j = camera_extrinsics1_.Inverse() * pts_imu_i;
  Eigen::Map<Eigen::Vector2d> residual(residuals);

  double dep_j = pts_camera_j.z();
  residual = (pts_camera_j / dep_j).head<2>() - obs_j_.head<2>();
  residual = sqrt_info_ * residual;

  if (jacobians) {
    Eigen::Matrix3d Ric0 = camera_extrinsics0_.rotation().toRotationMatrix();
    Eigen::Matrix3d Ric1 = camera_extrinsics1_.rotation().toRotationMatrix();
    Eigen::Matrix<double, 2, 3> reduce(2, 3);
    reduce << 1. / dep_j, 0, -pts_camera_j(0) / (dep_j * dep_j), 0, 1. / dep_j,
        -pts_camera_j(1) / (dep_j * dep_j);
    reduce = sqrt_info_ * reduce;

    if (jacobians[0]) {
      Eigen::Map<Eigen::Vector2d> jacobian_feature(jacobians[0]);
      jacobian_feature = reduce * Ric1.transpose() * Ric0 * obs_i_ * -1.0 / (inv_dep_i * inv_dep_i);
    }
  }

  return true;
}

Eigen::Vector2d CameraProjectionInverseDepthStereoFactor::CalcResidual(
    double const* const* parameters) {
  double inv_dep_i = parameters[0][0];

  Eigen::Vector3d pts_camera_i = obs_i_ / inv_dep_i;
  Eigen::Vector3d pts_imu_i = camera_extrinsics0_ * pts_camera_i;
  Eigen::Vector3d pts_camera_j = camera_extrinsics1_.Inverse() * pts_imu_i;

  Eigen::Vector2d residual;
  double dep_j = pts_camera_j.z();
  residual = (pts_camera_j / dep_j).head<2>() - obs_j_.head<2>();
  return sqrt_info_ * residual;
}

CameraProjectionInverseDepthMonoFactor::CameraProjectionInverseDepthMonoFactor(
    const Eigen::Vector3d& obs_i, const Eigen::Vector3d& obs_j,
    const math::Rigid3d& camera_extrinsics, const Eigen::Vector2d& weight)
    : obs_i_(obs_i), obs_j_(obs_j), camera_extrinsics_(camera_extrinsics) {
  sqrt_info_ << weight[0], 0, 0, weight[1];
}

bool CameraProjectionInverseDepthMonoFactor::Evaluate(double const* const* parameters,
                                                      double* residuals, double** jacobians) const {
  Eigen::Quaterniond Qi =
      dm::math::AngleVectorToRotation(Eigen::Map<const Eigen::Vector3d>(parameters[0]));
  Eigen::Map<const Eigen::Vector3d> Pi(parameters[0] + 3);
  Eigen::Quaterniond Qj =
      dm::math::AngleVectorToRotation(Eigen::Map<const Eigen::Vector3d>(parameters[1]));
  Eigen::Map<const Eigen::Vector3d> Pj(parameters[1] + 3);

  double inv_dep_i = parameters[2][0];

  Eigen::Vector3d pts_camera_i = obs_i_ / inv_dep_i;
  Eigen::Vector3d pts_imu_i = camera_extrinsics_ * pts_camera_i;
  Eigen::Vector3d pts_w = Qi * pts_imu_i + Pi;
  Eigen::Vector3d pts_imu_j = Qj.inverse() * (pts_w - Pj);
  Eigen::Vector3d pts_camera_j = camera_extrinsics_.Inverse() * pts_imu_j;
  Eigen::Map<Eigen::Vector2d> residual(residuals);

  double dep_j = pts_camera_j.z();
  residual = (pts_camera_j / dep_j).head<2>() - obs_j_.head<2>();

  residual = sqrt_info_ * residual;

  if (jacobians) {
    Eigen::Matrix3d Ri = Qi.toRotationMatrix();
    Eigen::Matrix3d Rj = Qj.toRotationMatrix();
    Eigen::Matrix3d Ric = camera_extrinsics_.rotation().toRotationMatrix();
    Eigen::Matrix<double, 2, 3> reduce(2, 3);
    reduce << 1. / dep_j, 0, -pts_camera_j(0) / (dep_j * dep_j), 0, 1. / dep_j,
        -pts_camera_j(1) / (dep_j * dep_j);
    reduce = sqrt_info_ * reduce;

    if (jacobians[0]) {
      Eigen::Map<Eigen::Matrix<double, 2, 6, Eigen::RowMajor>> jacobian_pose_i(jacobians[0]);
      Eigen::Matrix<double, 3, 6> jaco_i;
      jaco_i.leftCols<3>() =
          Ric.transpose() * Rj.transpose() * Ri * -dm::math::SkewSymmetric(pts_imu_i);
      jaco_i.rightCols<3>() = Ric.transpose() * Rj.transpose();
      jacobian_pose_i = reduce * jaco_i;
    }

    if (jacobians[1]) {
      Eigen::Map<Eigen::Matrix<double, 2, 6, Eigen::RowMajor>> jacobian_pose_j(jacobians[1]);
      Eigen::Matrix<double, 3, 6> jaco_j;
      jaco_j.leftCols<3>() = Ric.transpose() * dm::math::SkewSymmetric(pts_imu_j);
      jaco_j.rightCols<3>() = Ric.transpose() * -Rj.transpose();
      jacobian_pose_j = reduce * jaco_j;
    }
    if (jacobians[2]) {
      Eigen::Map<Eigen::Vector2d> jacobian_feature(jacobians[2]);
      jacobian_feature = reduce * Ric.transpose() * Rj.transpose() * Ri * Ric * obs_i_ * -1.0 /
                         (inv_dep_i * inv_dep_i);
    }
  }

  return true;
}

Eigen::Vector2d CameraProjectionInverseDepthMonoFactor::CalcResidual(
    double const* const* parameters) {
  Eigen::Quaterniond Qi =
      dm::math::AngleVectorToRotation(Eigen::Map<const Eigen::Vector3d>(parameters[0]));
  Eigen::Map<const Eigen::Vector3d> Pi(parameters[0] + 3);
  Eigen::Quaterniond Qj =
      dm::math::AngleVectorToRotation(Eigen::Map<const Eigen::Vector3d>(parameters[1]));
  Eigen::Map<const Eigen::Vector3d> Pj(parameters[1] + 3);

  double inv_dep_i = parameters[2][0];

  Eigen::Vector3d pts_camera_i = obs_i_ / inv_dep_i;
  Eigen::Vector3d pts_imu_i = camera_extrinsics_ * pts_camera_i;
  Eigen::Vector3d pts_w = Qi * pts_imu_i + Pi;
  Eigen::Vector3d pts_imu_j = Qj.inverse() * (pts_w - Pj);
  Eigen::Vector3d pts_camera_j = camera_extrinsics_.Inverse() * pts_imu_j;

  Eigen::Vector2d residual;
  double dep_j = pts_camera_j.z();
  residual = (pts_camera_j / dep_j).head<2>() - obs_j_.head<2>();
  return sqrt_info_ * residual;
}

CameraProjectionInverseDepthBinoFactor::CameraProjectionInverseDepthBinoFactor(
    const Eigen::Vector3d& obs_i, const Eigen::Vector3d& obs_j,
    const math::Rigid3d& camera_extrinsics0, const math::Rigid3d& camera_extrinsics1,
    const Eigen::Vector2d& weight)
    : obs_i_(obs_i),
      obs_j_(obs_j),
      camera_extrinsics0_(camera_extrinsics0),
      camera_extrinsics1_(camera_extrinsics1) {
  sqrt_info_ << weight[0], 0, 0, weight[1];
}

bool CameraProjectionInverseDepthBinoFactor::Evaluate(double const* const* parameters,
                                                      double* residuals, double** jacobians) const {
  Eigen::Quaterniond Qi =
      dm::math::AngleVectorToRotation(Eigen::Map<const Eigen::Vector3d>(parameters[0]));
  Eigen::Map<const Eigen::Vector3d> Pi(parameters[0] + 3);
  Eigen::Quaterniond Qj =
      dm::math::AngleVectorToRotation(Eigen::Map<const Eigen::Vector3d>(parameters[1]));
  Eigen::Map<const Eigen::Vector3d> Pj(parameters[1] + 3);

  double inv_dep_i = parameters[2][0];

  Eigen::Vector3d pts_camera_i = obs_i_ / inv_dep_i;
  Eigen::Vector3d pts_imu_i = camera_extrinsics0_ * pts_camera_i;
  Eigen::Vector3d pts_w = Qi * pts_imu_i + Pi;
  Eigen::Vector3d pts_imu_j = Qj.inverse() * (pts_w - Pj);
  Eigen::Vector3d pts_camera_j = camera_extrinsics1_.Inverse() * pts_imu_j;
  Eigen::Map<Eigen::Vector2d> residual(residuals);

  double dep_j = pts_camera_j.z();
  residual = (pts_camera_j / dep_j).head<2>() - obs_j_.head<2>();

  residual = sqrt_info_ * residual;

  if (jacobians) {
    Eigen::Matrix3d Ri = Qi.toRotationMatrix();
    Eigen::Matrix3d Rj = Qj.toRotationMatrix();
    Eigen::Matrix3d Ric0 = camera_extrinsics0_.rotation().toRotationMatrix();
    Eigen::Matrix3d Ric1 = camera_extrinsics1_.rotation().toRotationMatrix();
    Eigen::Matrix<double, 2, 3> reduce(2, 3);
    reduce << 1. / dep_j, 0, -pts_camera_j(0) / (dep_j * dep_j), 0, 1. / dep_j,
        -pts_camera_j(1) / (dep_j * dep_j);
    reduce = sqrt_info_ * reduce;

    if (jacobians[0]) {
      Eigen::Map<Eigen::Matrix<double, 2, 6, Eigen::RowMajor>> jacobian_pose_i(jacobians[0]);
      Eigen::Matrix<double, 3, 6> jaco_i;
      jaco_i.leftCols<3>() =
          Ric1.transpose() * Rj.transpose() * Ri * -dm::math::SkewSymmetric(pts_imu_i);
      jaco_i.rightCols<3>() = Ric1.transpose() * Rj.transpose();
      jacobian_pose_i = reduce * jaco_i;
    }

    if (jacobians[1]) {
      Eigen::Map<Eigen::Matrix<double, 2, 6, Eigen::RowMajor>> jacobian_pose_j(jacobians[1]);
      Eigen::Matrix<double, 3, 6> jaco_j;
      jaco_j.leftCols<3>() = Ric1.transpose() * dm::math::SkewSymmetric(pts_imu_j);
      jaco_j.rightCols<3>() = Ric1.transpose() * -Rj.transpose();
      jacobian_pose_j = reduce * jaco_j;
    }

    if (jacobians[2]) {
      Eigen::Map<Eigen::Vector2d> jacobian_feature(jacobians[2]);
      jacobian_feature = reduce * Ric1.transpose() * Rj.transpose() * Ri * Ric0 * obs_i_ * -1.0 /
                         (inv_dep_i * inv_dep_i);
    }
  }

  return true;
}

Eigen::Vector2d CameraProjectionInverseDepthBinoFactor::CalcResidual(
    double const* const* parameters) {
  Eigen::Quaterniond Qi =
      dm::math::AngleVectorToRotation(Eigen::Map<const Eigen::Vector3d>(parameters[0]));
  Eigen::Map<const Eigen::Vector3d> Pi(parameters[0] + 3);
  Eigen::Quaterniond Qj =
      dm::math::AngleVectorToRotation(Eigen::Map<const Eigen::Vector3d>(parameters[1]));
  Eigen::Map<const Eigen::Vector3d> Pj(parameters[1] + 3);

  double inv_dep_i = parameters[2][0];

  Eigen::Vector3d pts_camera_i = obs_i_ / inv_dep_i;
  Eigen::Vector3d pts_imu_i = camera_extrinsics0_ * pts_camera_i;
  Eigen::Vector3d pts_w = Qi * pts_imu_i + Pi;
  Eigen::Vector3d pts_imu_j = Qj.inverse() * (pts_w - Pj);
  Eigen::Vector3d pts_camera_j = camera_extrinsics1_.Inverse() * pts_imu_j;

  Eigen::Vector2d residual;
  double dep_j = pts_camera_j.z();
  residual = (pts_camera_j / dep_j).head<2>() - obs_j_.head<2>();
  return sqrt_info_ * residual;
}

}  // namespace vio
}  // namespace dm
