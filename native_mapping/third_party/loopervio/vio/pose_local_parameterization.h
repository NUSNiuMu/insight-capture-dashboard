#ifndef VIO_POSE_LOCAL_PARAMETERIZATION_H_
#define VIO_POSE_LOCAL_PARAMETERIZATION_H_
#pragma once

#include "Eigen/Dense"
#include "ceres/ceres.h"

namespace dm {
namespace vio {

class PoseManifold : public ceres::Manifold {
  virtual bool Plus(const double* x, const double* delta, double* x_plus_delta) const;
  virtual bool PlusJacobian(const double* x, double* jacobian) const;
  virtual int AmbientSize() const { return 6; }
  virtual int TangentSize() const { return 6; }
  virtual bool Minus(const double* y, const double* x, double* y_minus_x) const { return false; }
  virtual bool MinusJacobian(const double* x, double* jacobian) const { return false; }
};

class GravityManifold : public ceres::Manifold {
  virtual bool Plus(const double* x, const double* delta, double* x_plus_delta) const;
  virtual bool PlusJacobian(const double* x, double* jacobian) const;
  virtual int AmbientSize() const { return 3; }
  virtual int TangentSize() const { return 3; }
  virtual bool Minus(const double* y, const double* x, double* y_minus_x) const { return false; }
  virtual bool MinusJacobian(const double* x, double* jacobian) const { return false; }
};

// use dq * q
class YawPoseManifold : public ceres::Manifold {
 public:
  virtual bool Plus(const double* x, const double* delta, double* x_plus_delta) const;
  virtual bool PlusJacobian(const double* x, double* jacobian) const;
  virtual int AmbientSize() const { return 6; }
  virtual int TangentSize() const { return 4; }
  virtual bool Minus(const double* y, const double* x, double* y_minus_x) const { return false; }
  virtual bool MinusJacobian(const double* x, double* jacobian) const { return false; }
};


}  // namespace vio
}  // namespace dm
#endif  // VIO_POSE_LOCAL_PARAMETERIZATION_H_
