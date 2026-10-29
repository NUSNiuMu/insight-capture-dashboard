#ifndef VIO_COST_FUNCTIONS_RELATIVE_POSE_FACTOR_H_
#define VIO_COST_FUNCTIONS_RELATIVE_POSE_FACTOR_H_

#include <Eigen/Dense>

#include <vector>

#include "ceres/ceres.h"
#include "ceres/rotation.h"
#include "math/transformation_utils.h"
#ifdef CMAKE_BUILD
#include "../ext/rigid.h"
#else
#include "math/rigid.h"
#endif

namespace dm {
namespace vio {

class RelativeRotFactor : public ceres::SizedCostFunction<3, 6, 6> {
 public:
  RelativeRotFactor() = delete;
  explicit RelativeRotFactor(Eigen::Quaterniond Qij, Eigen::Matrix3d info, bool sqrt = false)
      : Qij_(Qij) {
    if (!sqrt)
      sqrt_info_ = Eigen::LLT<Eigen::Matrix3d>(info).matrixL().transpose();
    else
      sqrt_info_ = info;
  }

  virtual bool Evaluate(double const* const* parameters, double* residuals,
                        double** jacobians) const;

  EIGEN_MAKE_ALIGNED_OPERATOR_NEW

 private:
  Eigen::Quaterniond Qij_;
  Eigen::Matrix3d sqrt_info_;
};

class RelativePoseFactor : public ceres::SizedCostFunction<6, 6, 6> {
 public:
  RelativePoseFactor(const Eigen::Quaterniond& q_ij, const Eigen::Vector3d& t_ij,
                     const Eigen::Matrix<double, 6, 6>& info);

  RelativePoseFactor(const math::Rigid3d& T_ij, const Eigen::Matrix<double, 6, 6>& info);

  virtual bool Evaluate(double const* const* parameters, double* residuals,
                        double** jacobians) const;

  //  Eigen::Matrix<double, 6, 1> CalcResidual(double const* const* parameters);

  EIGEN_MAKE_ALIGNED_OPERATOR_NEW

 private:
  Eigen::Quaterniond q_ij_;
  Eigen::Vector3d t_ij_;
  Eigen::Matrix<double, 6, 6> sqrt_info_;
};


class LhsRelativePoseFactor : public ceres::SizedCostFunction<6, 6, 6> {
 public:
  LhsRelativePoseFactor(const Eigen::Quaterniond& q_ij, const Eigen::Vector3d& t_ij,
                     const Eigen::Matrix<double, 6, 6>& info);

  LhsRelativePoseFactor(const math::Rigid3d& T_ij, const Eigen::Matrix<double, 6, 6>& info);

  virtual bool Evaluate(double const* const* parameters, double* residuals,
                        double** jacobians) const;

  //  Eigen::Matrix<double, 6, 1> CalcResidual(double const* const* parameters);

  EIGEN_MAKE_ALIGNED_OPERATOR_NEW

 private:
  Eigen::Quaterniond q_ij_;
  Eigen::Vector3d t_ij_;
  Eigen::Matrix<double, 6, 6> sqrt_info_;
};



}  // namespace vio
}  // namespace dm

#endif  // VIO_COST_FUNCTIONS_RELATIVE_POSE_FACTOR_H_
