// Copyright 2021 DeepMirror Inc. All rights reserved.

#ifndef VIO_COST_FUNCTIONS_IMU_FACTOR_H_
#define VIO_COST_FUNCTIONS_IMU_FACTOR_H_

#include <Eigen/Dense>

#include <vector>

#include "ceres/ceres.h"
#include "ceres/rotation.h"
#include "vio/backend/imu_preintegration.h"
#include "math/transformation_utils.h"

namespace dm {
namespace vio {

class ImuMeasurementFactor : public ceres::SizedCostFunction<15, 6, 3, 3, 3, 6, 3, 3, 3, 3> {
 public:
  ImuMeasurementFactor() = delete;
  explicit ImuMeasurementFactor(dm::vio::ImuPreIntegration* pre_integration)
      : pre_integration_(pre_integration) {}
  virtual bool Evaluate(double const* const* parameters, double* residuals,
                        double** jacobians) const;
  Eigen::Matrix<double, 15, 1> CalcResidual(double const* const* parameters);

  EIGEN_MAKE_ALIGNED_OPERATOR_NEW

 private:
  dm::vio::ImuPreIntegration* pre_integration_;
};

class ScaleImuMeasurementFactor
    : public ceres::SizedCostFunction<15, /*QP*/6, /*V*/ 3, /*ba*/ 3, /*bg*/ 3, /*QP*/ 6,
                                      /*V*/ 3, /*ba*/ 3, /*bg*/ 3, /*G*/ 3, /*scale*/1> {
 public:
  ScaleImuMeasurementFactor() = delete;
  explicit ScaleImuMeasurementFactor(dm::vio::ImuPreIntegration* pre_integration)
      : pre_integration_(pre_integration) {}
  virtual bool Evaluate(double const* const* parameters, double* residuals,
                        double** jacobians) const;

 private:
  dm::vio::ImuPreIntegration* pre_integration_;
};

}  // namespace vio
}  // namespace dm

#endif  // VIO_COST_FUNCTIONS_IMU_FACTOR_H_
