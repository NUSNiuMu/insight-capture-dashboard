#ifndef VIO_INITIALIZER_VISUAL_IMU_ALIGNMENT_H_
#define VIO_INITIALIZER_VISUAL_IMU_ALIGNMENT_H_

#include <Eigen/Eigen>
#include <vector>
#include "vio/backend/sliding_window_params.h"

namespace dm {
namespace vio {
class VisualImuAlignment {
 public:
  VisualImuAlignment() {}

  VisualImuAlignment(const std::vector<dm::common::ImageUndistortion>& camera_intrinsics,
                     const std::vector<dm::math::Rigid3d>& camera_extrinsics, double gravity_norm,
                     SlidingWindowParams* params)
      : params_(params),
        camera_intrinsics_(camera_intrinsics),
        camera_extrinsics_(camera_extrinsics),
        gravity_norm_(gravity_norm) {}

  bool LinearSolve();

  double scale() { return scale_; }

 private:
  bool LinearAlignment(const Eigen::Vector3d& t_ic, const double& g_norm, Eigen::VectorXd* x);
  bool SolveGyroscopeBias();
  void RefineGravity(const Eigen::Vector3d& t_ic, const double& g_norm, Eigen::VectorXd* x);

  double ComputeCond(const Eigen::MatrixXd& A) {
    Eigen::JacobiSVD<Eigen::MatrixXd> svd(A, Eigen::ComputeThinU | Eigen::ComputeThinV);
    Eigen::MatrixXd U = svd.matrixU();
    Eigen::MatrixXd V = svd.matrixV();
    Eigen::VectorXd sv = svd.singularValues();
    double cond = svd.singularValues()[0] / svd.singularValues()(svd.singularValues().size() - 1);
    return cond;
  }

  SlidingWindowParams* params_;

  std::vector<dm::common::ImageUndistortion> camera_intrinsics_;
  // Pose of camera in IMU coordinate.
  std::vector<dm::math::Rigid3d> camera_extrinsics_;
  Eigen::Vector3d gravity_in_c0_;
  double gravity_norm_ = 9.801;
  double scale_ = 0;
};
}  // namespace vio
}  // namespace dm

#endif  // VIO_INITIALIZER_VISUAL_IMU_ALIGNMENT_H_
