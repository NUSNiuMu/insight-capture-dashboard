#ifndef CONFIG_VIO_CONFIG_H_
#define CONFIG_VIO_CONFIG_H_
#include <cstdint>
#include <memory>
#include <string>
#include "vio/backend/backend_config.h"
#include "vio/frontend/frontend_config.h"
namespace dm {
namespace vio {

struct VioConfig {
  using Ptr = std::shared_ptr<VioConfig>;
  VioConfig() = default;
  explicit VioConfig(const std::string& config_path);
  explicit VioConfig(const nlohmann::json& config_json);
  void ReadFromJson(const nlohmann::json& config_json);
  void WriteToJson(nlohmann::json* config_json) const;
  VioFrontendConfig frontend_config;
  VioBackendConfig backend_config;
  uint32_t cv_num_of_threads = 1;
  // Negative keeps OpenCV's default RNG state. A non-negative value makes
  // RANSAC deterministic in both the caller and stereo frontend threads.
  int32_t cv_rng_seed = -1;
  uint32_t debug_verbose_level = 0;
  bool write_session_bin = false;
  bool dump_vio_mcap = false;
  std::string session_path = "";
  bool enable_data_monitor = false;
  bool save_odom_pose = false;
  std::string odom_pose_path = "";
  uint32_t predict_period = 20;
};

}  // namespace vio
}  // namespace dm

#endif   // CONFIG_VIO_CONFIG_H_
