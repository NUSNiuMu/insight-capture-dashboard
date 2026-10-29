// Copyright 2021 DeepMirror Inc. All rights reserved.

#ifndef VIO_COST_FUNCTIONS_VECTOR_FACTOR_H_
#define VIO_COST_FUNCTIONS_VECTOR_FACTOR_H_

#include <Eigen/Dense>

#include "ceres/ceres.h"

namespace dm {
namespace vio {

class Vector3Error {
 public:
  explicit Vector3Error(const Eigen::Vector3d& weight) : target_(0.0, 0.0, 0.0), weight_(weight) {}
  Vector3Error(const Eigen::Vector3d& target, const Eigen::Vector3d& weight)
      : target_(target), weight_(weight) {}

  template <typename T>
  bool operator()(const T* const vec3, T* residuals) const;

  static ceres::CostFunction* CreateCostFunction(const Eigen::Vector3d& weight);
  static ceres::CostFunction* CreateCostFunction(const Eigen::Vector3d& target,
                                                 const Eigen::Vector3d& weight);

  EIGEN_MAKE_ALIGNED_OPERATOR_NEW

 private:
  Eigen::Vector3d target_;
  Eigen::Vector3d weight_;
};

class Vector3Factor : public ceres::SizedCostFunction<3, 3> {
 public:
  Vector3Factor(const Eigen::Vector3d& target, const Eigen::Vector3d& weight)
      : target_(target), weight_(weight) {}
  virtual bool Evaluate(double const* const* parameters, double* residuals,
                        double** jacobians) const;
  Eigen::Vector3d CalcResidual(double const* const* parameters) const;

  EIGEN_MAKE_ALIGNED_OPERATOR_NEW

 private:
  Eigen::Vector3d target_;
  Eigen::Vector3d weight_;
};

}  // namespace vio
}  // namespace dm

#endif  // VIO_COST_FUNCTIONS_VECTOR_FACTOR_H_
