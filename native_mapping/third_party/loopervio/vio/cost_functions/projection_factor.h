// Copyright 2021 DeepMirror Inc. All rights reserved.

#ifndef VIO_COST_FUNCTIONS_PROJECTION_FACTOR_H_
#define VIO_COST_FUNCTIONS_PROJECTION_FACTOR_H_

#include <Eigen/Dense>

#include <algorithm>
#include <vector>

#include "ceres/ceres.h"
#include "ceres/rotation.h"
#ifdef CMAKE_BUILD
#include "../ext/rigid.h"
#else
#include "math/rigid.h"
#endif
#include "math/transformation_utils.h"

namespace dm {
namespace vio {

// Camera projection error, assuming a pinhole camera model.
class CameraProjectionError {
 public:
  // "observation" must be normalized.
  CameraProjectionError(const math::Rigid3d& camera_extrinsics, const Eigen::Vector2d& observation,
                        const Eigen::Vector2d& weight)
      : camera_extrinsics_(camera_extrinsics), observation_(observation), weight_(weight) {}

  // For IMU pose, the first three parameters are the AngleAxis representation of its rotation,
  // and the next three parameters are the translation. Dimension of residuals is 2.
  template <typename T>
  bool operator()(const T* const imu_pose, const T* const point, T* residuals) const;

  static ceres::CostFunction* CreateCostFunction(const math::Rigid3d& camera_extrinsics,
                                                 const Eigen::Vector2d& observation,
                                                 const Eigen::Vector2d& weight);

  EIGEN_MAKE_ALIGNED_OPERATOR_NEW

 private:
  math::Rigid3d camera_extrinsics_;
  Eigen::Vector2d observation_;
  Eigen::Vector2d weight_;
};

class CameraProjectionFactor : public ceres::SizedCostFunction<2, 6, 3> {
 public:
  CameraProjectionFactor(const math::Rigid3d& camera_extrinsics, const Eigen::Vector2d& observation,
                         const Eigen::Vector2d& weight);
  CameraProjectionFactor(const math::Rigid3d& camera_extrinsics, const Eigen::Vector2d& observation,
                         const Eigen::Matrix2d& sqrt_info);
  virtual bool Evaluate(double const* const* parameters, double* residuals,
                        double** jacobians) const;
  Eigen::Vector2d CalcResidual(double const* const* parameters);

  EIGEN_MAKE_ALIGNED_OPERATOR_NEW

 private:
  math::Rigid3d camera_extrinsics_;
  Eigen::Vector2d observation_;
  Eigen::Matrix2d sqrt_info_;
};

class LhsCameraProjectionFactor : public ceres::SizedCostFunction<2, 6, 3> {
 public:
  LhsCameraProjectionFactor(const math::Rigid3d& camera_extrinsics,
                            const Eigen::Vector2d& observation, const Eigen::Vector2d& weight);
  LhsCameraProjectionFactor(const math::Rigid3d& camera_extrinsics,
                            const Eigen::Vector2d& observation, const Eigen::Matrix2d& sqrt_info);
  virtual bool Evaluate(double const* const* parameters, double* residuals,
                        double** jacobians) const;

  EIGEN_MAKE_ALIGNED_OPERATOR_NEW

 private:
  math::Rigid3d camera_extrinsics_;
  Eigen::Vector2d observation_;
  Eigen::Matrix2d sqrt_info_;
};

class RollingCameraProjectionFactor : public ceres::SizedCostFunction<2, 6, 3, 3> {
 public:
  RollingCameraProjectionFactor(const math::Rigid3d& camera_extrinsic,
                                const Eigen::Vector2d& observation,
                                const Eigen::Quaterniond& rolling_inc_q, float rolling_dt,
                                const Eigen::Vector2d& weight)
      : camera_extrinsic_(camera_extrinsic),
        observation_(observation),
        rolling_inc_q_(rolling_inc_q),
        rolling_dt_(rolling_dt) {
    sqrt_info_ << weight[0], 0, 0, weight[1];
  }

  virtual bool Evaluate(double const* const* parameters, double* residuals,
                        double** jacobians) const;

  Eigen::Vector2d CalcResidual(double const* const* parameters) const;

  EIGEN_MAKE_ALIGNED_OPERATOR_NEW

 private:
  math::Rigid3d camera_extrinsic_;
  Eigen::Vector2d observation_;
  Eigen::Matrix2d sqrt_info_;
  Eigen::Quaterniond rolling_inc_q_;
  double rolling_dt_;
};

class SimpRollingCameraProjectionFactor : public ceres::SizedCostFunction<2, 6, 3> {
 public:
  SimpRollingCameraProjectionFactor(const math::Rigid3d& camera_extrinsic,
                                    const Eigen::Vector2d& observation,
                                    const Eigen::Quaterniond& rolling_inc_q,
                                    const Eigen::Vector2d& weight)
      : camera_extrinsic_(camera_extrinsic),
        observation_(observation),
        rolling_inc_q_(rolling_inc_q) {
    sqrt_info_ << weight[0], 0, 0, weight[1];
  }

  virtual bool Evaluate(double const* const* parameters, double* residuals,
                        double** jacobians) const;

  Eigen::Vector2d CalcResidual(double const* const* parameters) const;

  EIGEN_MAKE_ALIGNED_OPERATOR_NEW

 private:
  math::Rigid3d camera_extrinsic_;
  Eigen::Vector2d observation_;
  Eigen::Matrix2d sqrt_info_;
  Eigen::Quaterniond rolling_inc_q_;
};


// A variant of Camera Projection Error.
// see https://deepmirror.atlassian.net/wiki/spaces/MTD/pages/523730945/
// Visual%2BInertial%2BOdometry%2BVIO%2Bbackend%2Bdesign#Modeling-camera-projection-cost
// for details.
class ConvexCameraProjectionError {
 public:
  // "observation" must be normalized.
  ConvexCameraProjectionError(const math::Rigid3d& camera_extrinsics,
                              const Eigen::Vector2d& observation, const Eigen::Vector3d& weight)
      : camera_extrinsics_(camera_extrinsics), observation_(observation), weight_(weight) {}

  // For IMU pose, the first three parameters are the AngleAxis representation of its rotation,
  // and the next three parameters are the translation. Dimension of residuals is 3.
  template <typename T>
  bool operator()(const T* const imu_pose, const T* const point, T* residuals) const;

  static ceres::CostFunction* CreateCostFunction(const math::Rigid3d& camera_extrinsics,
                                                 const Eigen::Vector2d& observation,
                                                 const Eigen::Vector3d& weight);

  EIGEN_MAKE_ALIGNED_OPERATOR_NEW

 private:
  math::Rigid3d camera_extrinsics_;
  Eigen::Vector2d observation_;
  Eigen::Vector3d weight_;
};

class ConvexCameraProjectionFactor : public ceres::SizedCostFunction<3, 6, 3> {
 public:
  ConvexCameraProjectionFactor(const math::Rigid3d& camera_extrinsics,
                               const Eigen::Vector2d& observation, const Eigen::Vector3d& weight);
  virtual bool Evaluate(double const* const* parameters, double* residuals,
                        double** jacobians) const;
  Eigen::Vector3d CalcResidual(double const* const* parameters);

  EIGEN_MAKE_ALIGNED_OPERATOR_NEW

 private:
  math::Rigid3d camera_extrinsics_;
  Eigen::Vector2d observation_;
  Eigen::Matrix3d sqrt_info_;
};

// This factor is based on only one frame and two cameras.
// Residual = (Tic1.Inverse() * Tic0 * (obs_i / inverse_depth) - obs_j).head<2>()
class CameraProjectionInverseDepthStereoFactor : public ceres::SizedCostFunction<2, 1> {
 public:
  CameraProjectionInverseDepthStereoFactor(const Eigen::Vector3d& obs_i,
                                           const Eigen::Vector3d& obs_j,
                                           const math::Rigid3d& camera_extrinsics0,
                                           const math::Rigid3d& camera_extrinsics1,
                                           const Eigen::Vector2d& weight);
  virtual bool Evaluate(double const* const* parameters, double* residuals,
                        double** jacobians) const;
  Eigen::Vector2d CalcResidual(double const* const* parameters);

  EIGEN_MAKE_ALIGNED_OPERATOR_NEW

 private:
  Eigen::Vector3d obs_i_, obs_j_;
  math::Rigid3d camera_extrinsics0_, camera_extrinsics1_;
  Eigen::Matrix2d sqrt_info_;
};

// This factor is based on two frame but only one camera.
// Residual = (Tic0.Inverse() * Twi1.Inverse() * Twi0 * Tic0 * (obs_i / inverse_depth) -
// obs_j).head<2>()
class CameraProjectionInverseDepthMonoFactor : public ceres::SizedCostFunction<2, 6, 6, 1> {
 public:
  CameraProjectionInverseDepthMonoFactor(const Eigen::Vector3d& obs_i, const Eigen::Vector3d& obs_j,
                                         const math::Rigid3d& camera_extrinsics,
                                         const Eigen::Vector2d& weight);
  virtual bool Evaluate(double const* const* parameters, double* residuals,
                        double** jacobians) const;
  Eigen::Vector2d CalcResidual(double const* const* parameters);

  EIGEN_MAKE_ALIGNED_OPERATOR_NEW

 private:
  Eigen::Vector3d obs_i_, obs_j_;
  math::Rigid3d camera_extrinsics_;
  Eigen::Matrix2d sqrt_info_;
};

// This factor is based on two frame and two cameras.
// Residual = (Tic1.Inverse() * Twi1.Inverse() * Twi0 * Tic0 * (obs_i / inverse_depth) -
// obs_j).head<2>()
class CameraProjectionInverseDepthBinoFactor : public ceres::SizedCostFunction<2, 6, 6, 1> {
 public:
  CameraProjectionInverseDepthBinoFactor(const Eigen::Vector3d& obs_i, const Eigen::Vector3d& obs_j,
                                         const math::Rigid3d& camera_extrinsics0,
                                         const math::Rigid3d& camera_extrinsics1,
                                         const Eigen::Vector2d& weight);
  virtual bool Evaluate(double const* const* parameters, double* residuals,
                        double** jacobians) const;
  Eigen::Vector2d CalcResidual(double const* const* parameters);

  EIGEN_MAKE_ALIGNED_OPERATOR_NEW

 private:
  Eigen::Vector3d obs_i_, obs_j_;
  math::Rigid3d camera_extrinsics0_, camera_extrinsics1_;
  Eigen::Matrix2d sqrt_info_;
};

}  // namespace vio
}  // namespace dm

#endif  // VIO_COST_FUNCTIONS_PROJECTION_FACTOR_H_
