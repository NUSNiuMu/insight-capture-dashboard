#ifndef CONFIG_SENSOR_CALIBRATION_H_
#define CONFIG_SENSOR_CALIBRATION_H_

#include <memory>
#include <optional>
#include <vector>
#include <string>
#include <unordered_map>
#ifdef CMAKE_BUILD
#include "../vio/ext/rigid.h"
#else
#include "math/rigid.h"
#endif
#include "nlohmann/json.hpp"

namespace dm {
namespace vio {

struct IMUCalibration {
  std::string imu_name;
  Eigen::Vector3d accelerator_noise;
  Eigen::Vector3d accelerator_bias_random_walk;
  Eigen::Vector3d gyroscope_noise;
  Eigen::Vector3d gyroscope_bias_random_walk;
  dm::math::Rigid3d T_imu_base;

  // Kalibr `--imu-models scale-misalignment` intrinsics, stored verbatim as Kalibr reports
  // them so the file stays directly comparable to the calibration report:
  //
  //     a_m = M_accel * a_true + b_a
  //     w_m = M_gyro * C_gyro_accel * w_true + A_gyro * a_true + b_g
  //
  // M_* are lower triangular (scale on the diagonal, misalignment below), C_gyro_accel is
  // the accelerometer-to-gyroscope rotation and A_gyro the gyroscope's g-sensitivity. The
  // defaults below are the "no intrinsics" case and reproduce the plain a_m - b_a model, so
  // a calibration file predating this field keeps its previous behaviour. Inverted once into
  // a vio::ImuModel via ImuModel::FromKalibr(); nothing consumes these matrices directly.
  Eigen::Matrix3d M_accel = Eigen::Matrix3d::Identity();
  Eigen::Matrix3d M_gyro = Eigen::Matrix3d::Identity();
  Eigen::Matrix3d C_gyro_accel = Eigen::Matrix3d::Identity();
  Eigen::Matrix3d A_gyro = Eigen::Matrix3d::Zero();
};

enum CameraModel { PINHOLE = 0, FISHEYE = 1 };

struct CameraCalibration {
  std::string camera_name;
  Eigen::Vector4d intrinsics;
  std::vector<double> distortion_coef;
  uint32_t width;
  uint32_t height;
  CameraModel camera_model = FISHEYE;
  dm::math::Rigid3d T_cam_base;
};

struct SensorCalibration {
  using Ptr = std::shared_ptr<SensorCalibration>;
  SensorCalibration() = default;
  explicit SensorCalibration(const std::string& calib_path);
  explicit SensorCalibration(const nlohmann::json& calib_json);
  void ReadFromJson(const nlohmann::json& calib_json);
  void WriteToJson(nlohmann::json* calib_json) const;

  std::string base_name;
  std::optional<std::vector<IMUCalibration>> imu_calibrations;
  std::vector<CameraCalibration> camera_calibrations;
};

}  // namespace vio
}  // namespace dm

#endif  // CONFIG_SENSOR_CALIBRATION_H_
