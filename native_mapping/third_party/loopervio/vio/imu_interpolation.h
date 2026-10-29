#ifndef VIO_IMU_INTERPOLATION_H_
#define VIO_IMU_INTERPOLATION_H_

#include <deque>
#include <utility>
#include "Eigen/Eigen"

namespace dm {
namespace vio {
class ImuInterpolation {
 public:
  enum Sensor {
    ACCELERATOR,
    GYROSCOPE,
  };

  using DataWithTimestamp = std::pair<uint64_t, Eigen::Vector3d>;

  ImuInterpolation() {}

  explicit ImuInterpolation(const Sensor& sensor) : ref_sensor_(sensor) {}

  void PushAcceleration(uint64_t timestamp, const Eigen::Vector3d& acc);

  void PushAngularVelocity(uint64_t timestamp, const Eigen::Vector3d& angular_velocity);

  bool TryGetNewImu(uint64_t* stamp, Eigen::Vector3d* acc, Eigen::Vector3d* gyr);

  Eigen::Vector3d Interpolation(uint64_t t, uint64_t t0, uint64_t t1, const Eigen::Vector3d& v0,
                                const Eigen::Vector3d& v1);
  Eigen::Vector3d Interpolation(double t, double t0, double t1, const Eigen::Vector3d& v0,
                                const Eigen::Vector3d& v1);

 private:
  void UpdateBuffer();
  std::deque<DataWithTimestamp> accelerations_;
  std::deque<DataWithTimestamp> angular_velocities_;
  Sensor ref_sensor_ = ACCELERATOR;
};

}  // namespace vio
}  // namespace dm

#endif  // VIO_IMU_INTERPOLATION_H_
