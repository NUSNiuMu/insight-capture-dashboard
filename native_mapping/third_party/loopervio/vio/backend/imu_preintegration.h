// Copyright 2021 DeepMirror Inc. All rights reserved.

#ifndef VIO_BACKEND_IMU_PREINTEGRATION_H_
#define VIO_BACKEND_IMU_PREINTEGRATION_H_

#include <vector>
#include <memory>
#include "Eigen/Dense"
#include "vio/imu_model.h"

namespace dm {
namespace vio {

class ImuPreIntegration {
 public:
  using Ptr = std::shared_ptr<ImuPreIntegration>;
  ImuPreIntegration() = delete;
  ImuPreIntegration(const Eigen::Vector3d& acc_0, const Eigen::Vector3d& gyr_0,
                    const Eigen::Vector3d& accel_bias, const Eigen::Vector3d& gyro_bias,
                    const double& acc_n, const double& gyr_n, const double& acc_w,
                    const double& gyr_w, const ImuModel& imu_model = ImuModel());

  void Propagate(double dt, const Eigen::Vector3d& acc, const Eigen::Vector3d& gyr,
                 bool repropagate = false);

  void Propagate(const ImuPreIntegration& pre_integration);

  void RePropagate(const Eigen::Vector3d& linearized_ba, const Eigen::Vector3d& linearized_bg);

  Eigen::Matrix<double, 15, 1> GetResiduals(const Eigen::Quaterniond& Qi, const Eigen::Vector3d& Pi,
                                           const Eigen::Vector3d& Vi, const Eigen::Vector3d& Bai,
                                           const Eigen::Vector3d& Bgi, const Eigen::Quaterniond& Qj,
                                           const Eigen::Vector3d& Pj, const Eigen::Vector3d& Vj,
                                           const Eigen::Vector3d& Baj, const Eigen::Vector3d& Bgj,
                                           const Eigen::Vector3d& G);

  void Reset(const Eigen::Vector3d& acc_0, const Eigen::Vector3d& gyr_0,
             const Eigen::Vector3d& accel_bias, const Eigen::Vector3d& gyro_bias);
  Eigen::Vector3d first_accel() const;
  Eigen::Vector3d first_gyro() const;
  Eigen::Vector3d accel_bias() const;
  Eigen::Vector3d gyro_bias() const;
  Eigen::Matrix<double, 15, 15> jacobian() const;
  Eigen::Matrix<double, 15, 15> sqrt_info() const;
  double sum_dt() const;
  Eigen::Quaterniond delta_q() const;
  Eigen::Vector3d delta_p() const;
  Eigen::Vector3d delta_v() const;
  std::vector<double> dt_buf() const;
  const std::vector<Eigen::Vector3d>& accel_buf() const;
  const std::vector<Eigen::Vector3d>& gyro_buf() const;


 private:
  void MidPointIntegration(double dt, const Eigen::Vector3d& acc_0, const Eigen::Vector3d& gyr_0,
                           const Eigen::Vector3d& acc_1, const Eigen::Vector3d& gyr_1,
                           const Eigen::Vector3d& delta_p, const Eigen::Quaterniond& delta_q,
                           const Eigen::Vector3d& delta_v, Eigen::Vector3d* result_delta_p,
                           Eigen::Quaterniond* result_delta_q, Eigen::Vector3d* result_delta_v,
                           bool update_jacobian);

  Eigen::Vector3d first_accel_, first_gyro_;
  Eigen::Vector3d last_accel_, last_gyro_;
  Eigen::Vector3d accel_bias_, gyro_bias_;
  // Turns (measurement, bias) into physical quantities; see vio/imu_model.h. Held by value
  // so the implicit copy ctor (used at data_types.cc:97) carries it along.
  ImuModel imu_model_;

  Eigen::Matrix<double, 15, 15> jacobian_;
  Eigen::Matrix<double, 15, 15> covariance_;
  Eigen::Matrix<double, 18, 18> noise_;
  double sum_dt_;
  Eigen::Vector3d delta_p_;
  Eigen::Quaterniond delta_q_;
  Eigen::Vector3d delta_v_;

  std::vector<double> dt_buf_;
  std::vector<Eigen::Vector3d> accel_buf_;
  std::vector<Eigen::Vector3d> gyro_buf_;
};

}  // namespace vio
}  // namespace dm

#endif  // VIO_BACKEND_IMU_PREINTEGRATION_H_
