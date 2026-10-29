#include "vio/backend/imu_preintegration.h"

namespace dm {
namespace vio {

ImuPreIntegration::ImuPreIntegration(const Eigen::Vector3d& acc_0, const Eigen::Vector3d& gyr_0,
                                     const Eigen::Vector3d& accel_bias,
                                     const Eigen::Vector3d& gyro_bias, const double& acc_n,
                                     const double& gyr_n, const double& acc_w, const double& gyr_w,
                                     const ImuModel& imu_model)
    : first_accel_{acc_0},
      first_gyro_{gyr_0},
      last_accel_{acc_0},
      last_gyro_{gyr_0},
      accel_bias_{accel_bias},
      gyro_bias_{gyro_bias},
      imu_model_{imu_model},
      jacobian_{Eigen::Matrix<double, 15, 15>::Identity()},
      covariance_{Eigen::Matrix<double, 15, 15>::Zero()},
      sum_dt_{0.0},
      delta_p_{Eigen::Vector3d::Zero()},
      delta_q_{Eigen::Quaterniond::Identity()},
      delta_v_{Eigen::Vector3d::Zero()}

{
  noise_ = Eigen::Matrix<double, 18, 18>::Zero();
  noise_.block<3, 3>(0, 0) = (acc_n * acc_n) * Eigen::Matrix3d::Identity();
  noise_.block<3, 3>(3, 3) = (gyr_n * gyr_n) * Eigen::Matrix3d::Identity();
  noise_.block<3, 3>(6, 6) = (acc_n * acc_n) * Eigen::Matrix3d::Identity();
  noise_.block<3, 3>(9, 9) = (gyr_n * gyr_n) * Eigen::Matrix3d::Identity();
  noise_.block<3, 3>(12, 12) = (acc_w * acc_w) * Eigen::Matrix3d::Identity();
  noise_.block<3, 3>(15, 15) = (gyr_w * gyr_w) * Eigen::Matrix3d::Identity();
}

void ImuPreIntegration::Propagate(double dt, const Eigen::Vector3d& acc, const Eigen::Vector3d& gyr,
                                  bool repropagate) {
  if (!repropagate) {
    dt_buf_.push_back(dt);
    accel_buf_.push_back(acc);
    gyro_buf_.push_back(gyr);
  }

  Eigen::Vector3d result_delta_p;
  Eigen::Quaterniond result_delta_q;
  Eigen::Vector3d result_delta_v;

  MidPointIntegration(dt, last_accel_, last_gyro_, acc, gyr, delta_p_, delta_q_, delta_v_,
                      &result_delta_p, &result_delta_q, &result_delta_v, true);

  delta_p_ = result_delta_p;
  delta_q_ = result_delta_q;
  delta_v_ = result_delta_v;
  delta_q_.normalize();
  sum_dt_ += dt;
  last_accel_ = acc;
  last_gyro_ = gyr;
}

void ImuPreIntegration::Propagate(const ImuPreIntegration& pre_integration) {
  auto dt_buf = pre_integration.dt_buf();
  auto acc_buf = pre_integration.accel_buf();
  auto gyr_buf = pre_integration.gyro_buf();
  for (size_t i = 0; i < dt_buf.size(); ++i) {
    dt_buf.push_back(dt_buf[i]);
    acc_buf.push_back(acc_buf[i]);
    gyr_buf.push_back(gyr_buf[i]);
    Propagate(dt_buf.back(), acc_buf.back(), gyr_buf.back());
  }
}

void ImuPreIntegration::RePropagate(const Eigen::Vector3d& accel_bias,
                                    const Eigen::Vector3d& gyro_bias) {
  sum_dt_ = 0.0;
  last_accel_ = first_accel_;
  last_gyro_ = first_gyro_;
  delta_p_.setZero();
  delta_q_.setIdentity();
  delta_v_.setZero();
  accel_bias_ = accel_bias;
  gyro_bias_ = gyro_bias;
  jacobian_.setIdentity();
  covariance_.setZero();
  for (int i = 0; i < static_cast<int>(dt_buf_.size()); i++)
    Propagate(dt_buf_[i], accel_buf_[i], gyro_buf_[i], true);
}

Eigen::Matrix<double, 15, 1> ImuPreIntegration::GetResiduals(
    const Eigen::Quaterniond& Qi, const Eigen::Vector3d& Pi, const Eigen::Vector3d& Vi,
    const Eigen::Vector3d& Bai, const Eigen::Vector3d& Bgi, const Eigen::Quaterniond& Qj,
    const Eigen::Vector3d& Pj, const Eigen::Vector3d& Vj, const Eigen::Vector3d& Baj,
    const Eigen::Vector3d& Bgj, const Eigen::Vector3d& G) {
  Eigen::Matrix<double, 15, 1> residuals;

  // pre-integration jacobian_: p, q, v, ba, bg
  Eigen::Matrix3d dp_dba = jacobian_.block<3, 3>(0, 9);
  Eigen::Matrix3d dp_dbg = jacobian_.block<3, 3>(0, 12);

  Eigen::Matrix3d dq_dbg = jacobian_.block<3, 3>(3, 12);

  Eigen::Matrix3d dv_dba = jacobian_.block<3, 3>(6, 9);
  Eigen::Matrix3d dv_dbg = jacobian_.block<3, 3>(6, 12);

  Eigen::Vector3d dba = Bai - accel_bias_;
  Eigen::Vector3d dbg = Bgi - gyro_bias_;

  Eigen::Vector3d tmp_delta_q = dq_dbg * dbg;
  Eigen::Quaterniond corrected_delta_q =
      delta_q_ *
      Eigen::Quaterniond(1.0, tmp_delta_q(0) / 2.0, tmp_delta_q(1) / 2.0, tmp_delta_q(2) / 2.0);
  Eigen::Vector3d corrected_delta_v = delta_v_ + dv_dba * dba + dv_dbg * dbg;
  Eigen::Vector3d corrected_delta_p = delta_p_ + dp_dba * dba + dp_dbg * dbg;

  // residual: p, q, v
  residuals.block<3, 1>(0, 0) =
      Qi.inverse() * (Pj - Pi - Vi * sum_dt_ - 0.5 * G * sum_dt_ * sum_dt_) - corrected_delta_p;
  residuals.block<3, 1>(3, 0) = 2 * (corrected_delta_q.inverse() * (Qi.inverse() * Qj)).vec();
  residuals.block<3, 1>(6, 0) = Qi.inverse() * (Vj - Vi - G * sum_dt_) - corrected_delta_v;
  residuals.block<3, 1>(9, 0) = Baj - Bai;
  residuals.block<3, 1>(12, 0) = Bgj - Bgi;
  return residuals;
}

void ImuPreIntegration::Reset(const Eigen::Vector3d& acc_0, const Eigen::Vector3d& gyr_0,
           const Eigen::Vector3d& accel_bias, const Eigen::Vector3d& gyro_bias) {
  sum_dt_ = 0.0;
  first_accel_ = acc_0;
  first_gyro_ = gyr_0;
  last_accel_ = acc_0;
  last_gyro_ = gyr_0;
  delta_p_.setZero();
  delta_q_.setIdentity();
  delta_v_.setZero();
  accel_bias_ = accel_bias;
  gyro_bias_ = gyro_bias;
  jacobian_.setIdentity();
  covariance_.setZero();
  dt_buf_.clear();
  accel_buf_.clear();
  gyro_buf_.clear();
}

Eigen::Vector3d ImuPreIntegration::first_accel() const { return first_accel_; }

Eigen::Vector3d ImuPreIntegration::first_gyro() const { return first_gyro_; }

Eigen::Vector3d ImuPreIntegration::accel_bias() const { return accel_bias_; }

Eigen::Vector3d ImuPreIntegration::gyro_bias() const { return gyro_bias_; }

Eigen::Matrix<double, 15, 15> ImuPreIntegration::jacobian() const { return jacobian_; }

Eigen::Matrix<double, 15, 15> ImuPreIntegration::sqrt_info() const {
  return Eigen::LLT<Eigen::Matrix<double, 15, 15>>(covariance_.inverse()).matrixL().transpose();
}

double ImuPreIntegration::sum_dt() const { return sum_dt_; }

Eigen::Quaterniond ImuPreIntegration::delta_q() const { return delta_q_; }

Eigen::Vector3d ImuPreIntegration::delta_p() const { return delta_p_; }

Eigen::Vector3d ImuPreIntegration::delta_v() const { return delta_v_; }

std::vector<double> ImuPreIntegration::dt_buf() const { return dt_buf_; }

const std::vector<Eigen::Vector3d>& ImuPreIntegration::accel_buf() const { return accel_buf_; }

const std::vector<Eigen::Vector3d>& ImuPreIntegration::gyro_buf() const { return gyro_buf_; }

void ImuPreIntegration::MidPointIntegration(
    double dt, const Eigen::Vector3d& acc_0, const Eigen::Vector3d& gyr_0,
    const Eigen::Vector3d& acc_1, const Eigen::Vector3d& gyr_1, const Eigen::Vector3d& delta_p,
    const Eigen::Quaterniond& delta_q, const Eigen::Vector3d& delta_v,
    Eigen::Vector3d* result_delta_p, Eigen::Quaterniond* result_delta_q,
    Eigen::Vector3d* result_delta_v, bool update_jacobian) {
  // Corrected specific force at both endpoints, then the midpoint angular rate. The gyro
  // correction consumes the corrected accel (g-sensitivity), hence this ordering.
  const Eigen::Vector3d a_0 = imu_model_.Accel(acc_0, accel_bias_);
  const Eigen::Vector3d a_1 = imu_model_.Accel(acc_1, accel_bias_);
  Eigen::Vector3d un_acc_0 = delta_q * a_0;
  Eigen::Vector3d un_gyr =
      imu_model_.Gyro(0.5 * (gyr_0 + gyr_1), gyro_bias_, 0.5 * (a_0 + a_1));
  *result_delta_q =
      delta_q * Eigen::Quaterniond(1, un_gyr(0) * dt / 2, un_gyr(1) * dt / 2, un_gyr(2) * dt / 2);
  Eigen::Vector3d un_acc_1 = (*result_delta_q) * a_1;
  Eigen::Vector3d un_acc = 0.5 * (un_acc_0 + un_acc_1);
  *result_delta_p = delta_p + delta_v * dt + 0.5 * un_acc * dt * dt;
  *result_delta_v = delta_v + un_acc * dt;

  if (update_jacobian) {
    const Eigen::Vector3d& w_x = un_gyr;
    const Eigen::Vector3d& a_0_x = a_0;
    const Eigen::Vector3d& a_1_x = a_1;
    Eigen::Matrix3d R_w_x, R_a_0_x, R_a_1_x;

    R_w_x << 0, -w_x(2), w_x(1), w_x(2), 0, -w_x(0), -w_x(1), w_x(0), 0;
    R_a_0_x << 0, -a_0_x(2), a_0_x(1), a_0_x(2), 0, -a_0_x(0), -a_0_x(1), a_0_x(0), 0;
    R_a_1_x << 0, -a_1_x(2), a_1_x(1), a_1_x(2), 0, -a_1_x(0), -a_1_x(1), a_1_x(0), 0;

    const Eigen::Matrix3d R0 = delta_q_.toRotationMatrix();
    const Eigen::Matrix3d R1 = (*result_delta_q).toRotationMatrix();

    // How the corrected measurements respond to the states and to the measurement noise.
    // Without intrinsics these are -I, -I, 0, +I, +0.5I and 0, which is what the classic
    // form below hard-codes; with intrinsics the accelerometer partials pick up Da and the
    // gyroscope ones RDw, and the g-sensitivity opens a path from the accelerometer bias and
    // the accelerometer noise into the angular rate.
    const Eigen::Matrix3d Ja_ba = imu_model_.dAccel_dAccelBias();
    const Eigen::Matrix3d Jw_bg = imu_model_.dGyro_dGyroBias();
    const Eigen::Matrix3d Jw_ba = imu_model_.dGyro_dAccelBias();
    const Eigen::Matrix3d& Ja_na = imu_model_.Da;
    // The midpoint rate averages both endpoints, so each endpoint's noise enters at half
    // weight -- and through Tg the accelerometer noise enters the angular rate too.
    const Eigen::Matrix3d Jw_ng = 0.5 * imu_model_.RDw;
    const Eigen::Matrix3d Jw_na = -0.5 * imu_model_.RDw * imu_model_.Tg * Ja_na;

    // Sensitivity of the propagated position/velocity to an orientation error at the end of
    // the interval. Everything that perturbs the angular rate reaches p and v through these,
    // so each such perturbation contributes S{p,v} * dt * (its partial of w_hat).
    const Eigen::Matrix3d Sp = -0.25 * R1 * R_a_1_x * dt * dt;
    const Eigen::Matrix3d Sv = -0.5 * R1 * R_a_1_x * dt;

    Eigen::MatrixXd F = Eigen::MatrixXd::Zero(15, 15);
    F.block<3, 3>(0, 0) = Eigen::Matrix3d::Identity();
    F.block<3, 3>(0, 3) =
        -0.25 * R0 * R_a_0_x * dt * dt + Sp * (Eigen::Matrix3d::Identity() - R_w_x * dt);
    F.block<3, 3>(0, 6) = Eigen::MatrixXd::Identity(3, 3) * dt;
    F.block<3, 3>(0, 9) = 0.25 * (R0 + R1) * Ja_ba * dt * dt + Sp * (dt * Jw_ba);
    F.block<3, 3>(0, 12) = Sp * (dt * Jw_bg);
    F.block<3, 3>(3, 3) = Eigen::Matrix3d::Identity() - R_w_x * dt;
    F.block<3, 3>(3, 9) = dt * Jw_ba;
    F.block<3, 3>(3, 12) = dt * Jw_bg;
    F.block<3, 3>(6, 3) =
        -0.5 * R0 * R_a_0_x * dt + Sv * (Eigen::Matrix3d::Identity() - R_w_x * dt);
    F.block<3, 3>(6, 6) = Eigen::Matrix3d::Identity();
    F.block<3, 3>(6, 9) = 0.5 * (R0 + R1) * Ja_ba * dt + Sv * (dt * Jw_ba);
    F.block<3, 3>(6, 12) = Sv * (dt * Jw_bg);
    F.block<3, 3>(9, 9) = Eigen::Matrix3d::Identity();
    F.block<3, 3>(12, 12) = Eigen::Matrix3d::Identity();

    // Noise order: [n_a0, n_g0, n_a1, n_g1, n_ba, n_bg].
    Eigen::MatrixXd G = Eigen::MatrixXd::Zero(15, 18);
    G.block<3, 3>(0, 0) = 0.25 * R0 * Ja_na * dt * dt + Sp * (dt * Jw_na);
    G.block<3, 3>(0, 3) = Sp * (dt * Jw_ng);
    G.block<3, 3>(0, 6) = 0.25 * R1 * Ja_na * dt * dt + Sp * (dt * Jw_na);
    G.block<3, 3>(0, 9) = G.block<3, 3>(0, 3);
    G.block<3, 3>(3, 0) = dt * Jw_na;
    G.block<3, 3>(3, 3) = dt * Jw_ng;
    G.block<3, 3>(3, 6) = G.block<3, 3>(3, 0);
    G.block<3, 3>(3, 9) = G.block<3, 3>(3, 3);
    G.block<3, 3>(6, 0) = 0.5 * R0 * Ja_na * dt + Sv * (dt * Jw_na);
    G.block<3, 3>(6, 3) = Sv * (dt * Jw_ng);
    G.block<3, 3>(6, 6) = 0.5 * R1 * Ja_na * dt + Sv * (dt * Jw_na);
    G.block<3, 3>(6, 9) = G.block<3, 3>(6, 3);
    G.block<3, 3>(9, 12) = Eigen::MatrixXd::Identity(3, 3) * dt;
    G.block<3, 3>(12, 15) = Eigen::MatrixXd::Identity(3, 3) * dt;

    jacobian_ = F * jacobian_;
    covariance_ = F * covariance_ * F.transpose() + G * noise_ * G.transpose();
  }
}

}  // namespace vio
}  // namespace dm
