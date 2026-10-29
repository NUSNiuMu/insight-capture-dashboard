#include "config/sensor_calibration.h"
#include <fstream>
#include "glog/logging.h"

namespace dm {
namespace vio {
namespace {

// Reads a 3x3 matrix stored row-major as a flat 9-element array. Leaves *out untouched when
// the key is absent, so every intrinsic matrix falls back to its own default independently.
// A present-but-malformed entry is a corrupt calibration, not an old one, so it throws.
void ReadMatrix3(const nlohmann::json& parent, const std::string& key, Eigen::Matrix3d* out) {
  if (!parent.contains(key)) return;
  const auto& values = parent[key];
  if (!values.is_array() || values.size() != 9u) {
    throw std::runtime_error("imu_calibrations intrinsics '" + key +
                             "' must be a row-major array of 9 numbers.");
  }
  for (int r = 0; r < 3; ++r) {
    for (int c = 0; c < 3; ++c) {
      (*out)(r, c) = values[r * 3 + c].get<double>();
    }
  }
}

// Row-major flat array, matching ReadMatrix3.
std::vector<double> Matrix3ToJson(const Eigen::Matrix3d& m) {
  std::vector<double> values(9);
  for (int r = 0; r < 3; ++r) {
    for (int c = 0; c < 3; ++c) {
      values[r * 3 + c] = m(r, c);
    }
  }
  return values;
}

}  // namespace

SensorCalibration::SensorCalibration(const std::string& calib_path) {
  std::ifstream file(calib_path);
  if (!file.is_open()) {
    throw std::runtime_error("Can not open the calibration file. " + calib_path);
  }
  nlohmann::json calib_json;
  file >> calib_json;
  file.close();
  ReadFromJson(calib_json);
}

SensorCalibration::SensorCalibration(const nlohmann::json& calib_json) { ReadFromJson(calib_json); }

void SensorCalibration::ReadFromJson(const nlohmann::json& calib_json) {
  if (calib_json.contains("base_name")) {
    base_name = calib_json.value("base_name", "IMU");
  } else {
    throw std::runtime_error("The base sensor name is necessary in the calibration file.");
  }

  if (calib_json.contains("imu_calibrations")) {
    std::vector<IMUCalibration> _imu_calibrations(calib_json["imu_calibrations"].size());
    try {
      for (size_t i = 0; i < _imu_calibrations.size(); ++i) {
        auto imu_calibration = calib_json["imu_calibrations"][i];
        _imu_calibrations[i].imu_name = imu_calibration["imu_name"];
        // intrinsics
        auto intrinsics = imu_calibration["intrinsics"];
        auto accel_noise = intrinsics["accelerator_noise"];
        _imu_calibrations[i].accelerator_noise =
            Eigen::Vector3d(accel_noise["x"], accel_noise["y"], accel_noise["z"]);
        auto accel_bias_random_walk = intrinsics["accelerator_bias_random_walk"];
        _imu_calibrations[i].accelerator_bias_random_walk = Eigen::Vector3d(
            accel_bias_random_walk["x"], accel_bias_random_walk["y"], accel_bias_random_walk["z"]);
        auto gyro_noise = intrinsics["gyroscope_noise"];
        _imu_calibrations[i].gyroscope_noise =
            Eigen::Vector3d(gyro_noise["x"], gyro_noise["y"], gyro_noise["z"]);
        auto gyro_bias_random_walk = intrinsics["gyroscope_bias_random_walk"];
        _imu_calibrations[i].gyroscope_bias_random_walk = Eigen::Vector3d(
            gyro_bias_random_walk["x"], gyro_bias_random_walk["y"], gyro_bias_random_walk["z"]);
        // Optional Kalibr scale-misalignment intrinsics. Absent (or partially present) means
        // "no intrinsics": each matrix independently keeps its identity/zero default, which
        // reproduces the behaviour of calibration files written before this field existed.
        ReadMatrix3(intrinsics, "accelerator_scale_misalignment", &_imu_calibrations[i].M_accel);
        ReadMatrix3(intrinsics, "gyroscope_scale_misalignment", &_imu_calibrations[i].M_gyro);
        ReadMatrix3(intrinsics, "gyroscope_to_accelerator_rotation",
                    &_imu_calibrations[i].C_gyro_accel);
        ReadMatrix3(intrinsics, "gyroscope_g_sensitivity", &_imu_calibrations[i].A_gyro);
        // extrinsics
        auto translation = imu_calibration["extrinsics"]["pose_base_in_sensor"]["translation"];
        auto rotation = imu_calibration["extrinsics"]["pose_base_in_sensor"]["rotation"];
        _imu_calibrations[i].T_imu_base = dm::math::Rigid3d(
            Eigen::Vector3d(translation.value("x", 0.0), translation.value("y", 0.0),
                            translation.value("z", 0.0)),
            Eigen::Quaterniond(rotation.value("w", 1.0), rotation.value("x", 0.0),
                               rotation.value("y", 0.0), rotation.value("z", 0.0)));
        if (!_imu_calibrations[i].T_imu_base.IsValid()) {
          throw std::runtime_error("The extrinsics in imu_calibrations is invalid.");
        }
      }
    } catch (const std::exception& e) {
      LOG(ERROR) << "imu_calibrations is missing some necessary fields";
      throw;
    }
    imu_calibrations = _imu_calibrations;
  }

  if (calib_json.contains("camera_calibrations")) {
    camera_calibrations.resize(calib_json["camera_calibrations"].size());
    try {
      for (size_t i = 0; i < camera_calibrations.size(); ++i) {
        auto camera_calibration = calib_json["camera_calibrations"][i];
        camera_calibrations[i].camera_name = camera_calibration["camera_name"];
        // intrinsics
        auto intrinsics = camera_calibration["intrinsics"];
        camera_calibrations[i].intrinsics =
            Eigen::Vector4d(intrinsics["fx"], intrinsics["fy"], intrinsics["cx"], intrinsics["cy"]);
        camera_calibrations[i].distortion_coef.resize(intrinsics["distortion_coef"].size());
        for (size_t j = 0; j < intrinsics["distortion_coef"].size(); ++j) {
          camera_calibrations[i].distortion_coef[j] = intrinsics["distortion_coef"][j];
        }
        camera_calibrations[i].width = intrinsics["width"];
        camera_calibrations[i].height = intrinsics["height"];
        if (intrinsics["camera_model"] == "PINHOLE") {
          camera_calibrations[i].camera_model = PINHOLE;
        } else if (intrinsics["camera_model"] == "FISHEYE") {
          camera_calibrations[i].camera_model = FISHEYE;
        } else {
          throw std::runtime_error("camera_model has invalid type.");
        }
        // extrinsics
        auto translation = camera_calibration["extrinsics"]["pose_base_in_sensor"]["translation"];
        auto rotation = camera_calibration["extrinsics"]["pose_base_in_sensor"]["rotation"];
        camera_calibrations[i].T_cam_base = dm::math::Rigid3d(
            Eigen::Vector3d(translation.value("x", 0.0), translation.value("y", 0.0),
                            translation.value("z", 0.0)),
            Eigen::Quaterniond(rotation.value("w", 1.0), rotation.value("x", 0.0),
                               rotation.value("y", 0.0), rotation.value("z", 0.0)));
        if (!camera_calibrations[i].T_cam_base.IsValid()) {
          throw std::runtime_error("The extrinsics in camera_calibrations is invalid.");
        }
      }
    } catch (const std::exception& e) {
      LOG(ERROR) << "camera_calibrations is missing some necessary fields";
      throw;
    }
  } else {
    throw std::runtime_error("The camera_calibrations is necessary in the calibration file.");
  }
}

void SensorCalibration::WriteToJson(nlohmann::json* calib_json) const {
  (*calib_json)["base_name"] = base_name;

  if (imu_calibrations.has_value()) {
    std::vector<nlohmann::json> imu_calibrations_json;
    for (const auto& imu_calib : imu_calibrations.value()) {
      nlohmann::json imu_json;
      imu_json["imu_name"] = imu_calib.imu_name;
      imu_json["intrinsics"]["accelerator_noise"] = {{"x", imu_calib.accelerator_noise.x()},
                                                     {"y", imu_calib.accelerator_noise.y()},
                                                     {"z", imu_calib.accelerator_noise.z()}};
      imu_json["intrinsics"]["accelerator_bias_random_walk"] = {
          {"x", imu_calib.accelerator_bias_random_walk.x()},
          {"y", imu_calib.accelerator_bias_random_walk.y()},
          {"z", imu_calib.accelerator_bias_random_walk.z()}};
      imu_json["intrinsics"]["gyroscope_noise"] = {{"x", imu_calib.gyroscope_noise.x()},
                                                   {"y", imu_calib.gyroscope_noise.y()},
                                                   {"z", imu_calib.gyroscope_noise.z()}};
      imu_json["intrinsics"]["gyroscope_bias_random_walk"] = {
          {"x", imu_calib.gyroscope_bias_random_walk.x()},
          {"y", imu_calib.gyroscope_bias_random_walk.y()},
          {"z", imu_calib.gyroscope_bias_random_walk.z()}};
      imu_json["intrinsics"]["accelerator_scale_misalignment"] = Matrix3ToJson(imu_calib.M_accel);
      imu_json["intrinsics"]["gyroscope_scale_misalignment"] = Matrix3ToJson(imu_calib.M_gyro);
      imu_json["intrinsics"]["gyroscope_to_accelerator_rotation"] =
          Matrix3ToJson(imu_calib.C_gyro_accel);
      imu_json["intrinsics"]["gyroscope_g_sensitivity"] = Matrix3ToJson(imu_calib.A_gyro);
      imu_json["extrinsics"]["pose_base_in_sensor"]["translation"] = {
          {"x", imu_calib.T_imu_base.translation().x()},
          {"y", imu_calib.T_imu_base.translation().y()},
          {"z", imu_calib.T_imu_base.translation().z()}};
      imu_json["extrinsics"]["pose_base_in_sensor"]["rotation"] = {
          {"x", imu_calib.T_imu_base.rotation().x()},
          {"y", imu_calib.T_imu_base.rotation().y()},
          {"z", imu_calib.T_imu_base.rotation().z()},
          {"w", imu_calib.T_imu_base.rotation().w()}};
      imu_calibrations_json.push_back(imu_json);
    }
    (*calib_json)["imu_calibrations"] = imu_calibrations_json;
  }

  std::vector<nlohmann::json> camera_calibrations_json;
  for (const auto& cam_calib : camera_calibrations) {
    nlohmann::json cam_json;
    cam_json["camera_name"] = cam_calib.camera_name;
    cam_json["intrinsics"]["fx"] = cam_calib.intrinsics[0];
    cam_json["intrinsics"]["fy"] = cam_calib.intrinsics[1];
    cam_json["intrinsics"]["cx"] = cam_calib.intrinsics[2];
    cam_json["intrinsics"]["cy"] = cam_calib.intrinsics[3];
    cam_json["intrinsics"]["distortion_coef"] = cam_calib.distortion_coef;
    cam_json["intrinsics"]["width"] = cam_calib.width;
    cam_json["intrinsics"]["height"] = cam_calib.height;
    if (cam_calib.camera_model == PINHOLE) {
      cam_json["intrinsics"]["camera_model"] = "PINHOLE";
    } else if (cam_calib.camera_model == FISHEYE) {
      cam_json["intrinsics"]["camera_model"] = "FISHEYE";
    } else {
      throw std::runtime_error("camera_model has invalid type.");
    }
    cam_json["extrinsics"]["pose_base_in_sensor"]["translation"] = {
        {"x", cam_calib.T_cam_base.translation().x()},
        {"y", cam_calib.T_cam_base.translation().y()},
        {"z", cam_calib.T_cam_base.translation().z()}};
    cam_json["extrinsics"]["pose_base_in_sensor"]["rotation"] = {
        {"x", cam_calib.T_cam_base.rotation().x()},
        {"y", cam_calib.T_cam_base.rotation().y()},
        {"z", cam_calib.T_cam_base.rotation().z()},
        {"w", cam_calib.T_cam_base.rotation().w()}};
    camera_calibrations_json.push_back(cam_json);
  }
  (*calib_json)["camera_calibrations"] = camera_calibrations_json;
}

}  // namespace vio
}  // namespace dm
