#include "vio/cost_functions/relative_pose_factor.h"

namespace dm {
namespace vio {

bool RelativeRotFactor::Evaluate(double const* const* parameters, double* residuals,
                                 double** jacobians) const {
  Eigen::Quaterniond Qi = dm::math::AngleVectorToRotation(
      Eigen::Vector3d(parameters[0][0], parameters[0][1], parameters[0][2]));
  Eigen::Quaterniond Qj = dm::math::AngleVectorToRotation(
      Eigen::Vector3d(parameters[1][0], parameters[1][1], parameters[1][2]));

  Eigen::Map<Eigen::Vector3d> v_residuals(residuals);
  Eigen::Quaterniond Qij_inv = Qij_.inverse();

  Eigen::Quaterniond Qij = Qi.inverse() * Qj;
  v_residuals = 2 * (Qij_inv * Qij).vec();
  v_residuals.applyOnTheLeft(sqrt_info_);

  if (jacobians) {
    if (jacobians[0]) {
      Eigen::Map<Eigen::Matrix<double, 3, 6, Eigen::RowMajor>> J(jacobians[0]);
      J.setZero();
      J.block<3, 3>(0, 0) =
          -(math::QuaternionLeft(Qij_inv) * math::QuaternionRight(Qij)).bottomRightCorner<3, 3>();
      J.applyOnTheLeft(sqrt_info_);
    }
    if (jacobians[1]) {
      Eigen::Map<Eigen::Matrix<double, 3, 6, Eigen::RowMajor>> J(jacobians[1]);
      J.setZero();
      J.block<3, 3>(0, 0) = dm::math::QuaternionLeft(Qij_inv * Qij).bottomRightCorner<3, 3>();
      J.applyOnTheLeft(sqrt_info_);
    }
  }
  return true;
}

RelativePoseFactor::RelativePoseFactor(const Eigen::Quaterniond& q_ij, const Eigen::Vector3d& t_ij,
                                       const Eigen::Matrix<double, 6, 6>& info)
    : q_ij_(q_ij), t_ij_(t_ij) {
  sqrt_info_ = info.llt().matrixL().transpose();
}

RelativePoseFactor::RelativePoseFactor(const math::Rigid3d& T_ij,
                                       const Eigen::Matrix<double, 6, 6>& info) {
  q_ij_ = T_ij.rotation();
  t_ij_ = T_ij.translation();
  sqrt_info_ = info.llt().matrixL().transpose();
}

bool RelativePoseFactor::Evaluate(double const* const* parameters, double* residuals,
                                  double** jacobians) const {
  // Tij * (Tj)^(-1) * Ti
  Eigen::Map<const Eigen::Vector3d> ri(parameters[0]);
  Eigen::Map<const Eigen::Vector3d> rj(parameters[1]);
  Eigen::Map<const Eigen::Vector3d> ti(parameters[0] + 3);
  Eigen::Map<const Eigen::Vector3d> tj(parameters[1] + 3);

  Eigen::Quaterniond qi = dm::math::AngleVectorToRotation(ri);
  Eigen::Quaterniond qj = dm::math::AngleVectorToRotation(rj);

  const Eigen::Quaterniond qji = qj.conjugate() * qi;
  const Eigen::Vector3d tji = qj.conjugate() * (ti - tj);

  const Eigen::Quaterniond res_q = q_ij_ * qji;
  const Eigen::Vector3d res_t = q_ij_ * tji + t_ij_;

  Eigen::Map<Eigen::Matrix<double, 6, 1>> residuals_vec(residuals);
  residuals_vec.head(3) = 2.0 * res_q.vec();
  residuals_vec.tail(3) = res_t;

  residuals_vec.applyOnTheLeft(sqrt_info_);

  if (jacobians) {
    const Eigen::Matrix3d dq_qj_inv_R = (q_ij_ * qj.conjugate()).toRotationMatrix();

    if (jacobians[0]) {
      Eigen::Map<Eigen::Matrix<double, 6, 6, Eigen::RowMajor>> jacobian_i(jacobians[0]);
      jacobian_i.setZero();

      jacobian_i.block<3, 3>(0, 0) = dm::math::QuaternionLeft(res_q).block<3, 3>(1, 1);
      jacobian_i.block<3, 3>(3, 3) = dq_qj_inv_R;

      jacobian_i.applyOnTheLeft(sqrt_info_);
    }
    if (jacobians[1]) {
      Eigen::Map<Eigen::Matrix<double, 6, 6, Eigen::RowMajor>> jacobian_j(jacobians[1]);
      jacobian_j.setZero();

      jacobian_j.block<3, 3>(0, 0) =
          -(dm::math::QuaternionLeft(q_ij_) * dm::math::QuaternionRight(qji)).block<3, 3>(1, 1);

      jacobian_j.block<3, 3>(3, 0) = q_ij_.toRotationMatrix() * dm::math::SkewSymmetric(tji);
      jacobian_j.block<3, 3>(3, 3) = -dq_qj_inv_R;
      jacobian_j.applyOnTheLeft(sqrt_info_);
    }
  }

  return true;
}

LhsRelativePoseFactor::LhsRelativePoseFactor(const Eigen::Quaterniond& q_ij,
                                             const Eigen::Vector3d& t_ij,
                                             const Eigen::Matrix<double, 6, 6>& info)
    : q_ij_(q_ij), t_ij_(t_ij) {
  sqrt_info_ = info.llt().matrixL().transpose();
}

LhsRelativePoseFactor::LhsRelativePoseFactor(const math::Rigid3d& T_ij,
                                             const Eigen::Matrix<double, 6, 6>& info) {
  q_ij_ = T_ij.rotation();
  t_ij_ = T_ij.translation();
  sqrt_info_ = info.llt().matrixL().transpose();
}

bool LhsRelativePoseFactor::Evaluate(double const* const* parameters, double* residuals,
                                     double** jacobians) const {
  // Tij * (Tj)^(-1) * Ti
  Eigen::Map<const Eigen::Vector3d> ri(parameters[0]);
  Eigen::Map<const Eigen::Vector3d> rj(parameters[1]);
  Eigen::Map<const Eigen::Vector3d> ti(parameters[0] + 3);
  Eigen::Map<const Eigen::Vector3d> tj(parameters[1] + 3);

  Eigen::Quaterniond qi = dm::math::AngleVectorToRotation(ri);
  Eigen::Quaterniond qj = dm::math::AngleVectorToRotation(rj);

  const Eigen::Quaterniond qji = qj.conjugate() * qi;
  const Eigen::Vector3d tji = qj.conjugate() * (ti - tj);

  const Eigen::Quaterniond res_q = q_ij_ * qji;
  const Eigen::Vector3d res_t = q_ij_ * tji + t_ij_;

  Eigen::Map<Eigen::Matrix<double, 6, 1>> residuals_vec(residuals);
  residuals_vec.head(3) = 2.0 * res_q.vec();
  residuals_vec.tail(3) = res_t;

  residuals_vec.applyOnTheLeft(sqrt_info_);

  if (jacobians) {
    const Eigen::Matrix3d dq_qj_inv_R = (q_ij_ * qj.conjugate()).toRotationMatrix();

    Eigen::Matrix3d Jq = dm::math::QuaternionLeft(q_ij_ * qj.conjugate()).block<3, 3>(1, 1) *
                         dm::math::QuaternionRight(qi).block<3, 3>(1, 1);

    if (jacobians[0]) {
      Eigen::Map<Eigen::Matrix<double, 6, 6, Eigen::RowMajor>> jacobian_i(jacobians[0]);
      jacobian_i.setZero();

      jacobian_i.block<3, 3>(0, 0) = Jq;
      jacobian_i.block<3, 3>(3, 3) = dq_qj_inv_R;
      jacobian_i.applyOnTheLeft(sqrt_info_);
    }
    if (jacobians[1]) {
      Eigen::Map<Eigen::Matrix<double, 6, 6, Eigen::RowMajor>> jacobian_j(jacobians[1]);
      jacobian_j.setZero();

      jacobian_j.block<3, 3>(0, 0) = -Jq;

      jacobian_j.block<3, 3>(3, 0) =
          (q_ij_ * qj.conjugate()).toRotationMatrix() * dm::math::SkewSymmetric(ti - tj);
      jacobian_j.block<3, 3>(3, 3) = -dq_qj_inv_R;
      jacobian_j.applyOnTheLeft(sqrt_info_);
    }
  }

  return true;
}

}  // namespace vio
}  // namespace dm
