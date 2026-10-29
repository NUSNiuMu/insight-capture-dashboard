#ifndef VIO_BACKEND_BACKEND_CONFIG_H_
#define VIO_BACKEND_BACKEND_CONFIG_H_

#include <optional>
#include <string>
#include "vio/data_types.h"
#include "nlohmann/json.hpp"

namespace dm {
namespace vio {

struct TrackingLostConfig {
  // MSCKF/SLAM/SLAMDelayInit update as the visual_constraint.
  uint32_t non_continuous_visual_constraint_threshold = 30;
  // If consecutive updates fail and the estimated speed exceeds the threshold, then it is lost.
  float tracking_lost_velocity_threshold = 0.5;
};

struct InitializerConfig {
  // Use the dynamic initialization methods.
  bool enable_dynamic_init = false;
  // Use the static initialization methods.
  bool enable_static_init = true;
  uint32_t static_min_feat_num = 30;
  float static_max_parallax = 2.0;
};

struct RelocalizerWithMapConfig {
  // Reject estimation when velocity excceeds the threshold in m/s.
  float velocity_rejection_threshold = 3.0;
  // Candidates that are used for initial PnP stage
  uint32_t max_retrieval_pnp_candidate = 5;
  // Candidates that are used for later pose refinement
  uint32_t max_retrieval_refinement_candidate = 5;
  // Smaller the more similar
  float max_retrieval_score = 0.05;
  // Min num of matched features after PnP considered as localized
  uint32_t min_pnp_matches = 20;
  // Min eigen ratio of inlier matched features after PnP. Denote V as
  // [v1, v2, ...], where vi is the homogeneous normalized position in the
  // image [u, v, 1]^T. Denote the eigen values of V as sig1, sig2 and sig3
  // in ascending order. Eigen ratio is defined to measure the "sparsity" of
  // the localized 2D features as: sig1^2 + sig2^2 / sig1^2 + sig2^2 + sig3^2, .
  float min_pnp_eigen_ratio = 0.05;
  // Minimum time interval for velocity estimation before two consecutive
  // relocalization results in ms.
  uint32_t relocalization_min_interval_ms = 145;
  // Maximum time interval for velocity estimation before two consecutive
  // relocalization results in ms.
  uint32_t relocalization_max_interval_ms = 500;
};

struct ProjectionNoise {
  float x = 1.0;
  float y = 1.0;
};

// Config for KeyFrameStateEstimator. This config specifies the window sizes,
// number of ceres iterations, weight parameters on the cost functions, and
// other relevant parameters of a KeyFrameStateEstimator.
struct VioBackendConfig {
  VioBackendConfig() = default;
  explicit VioBackendConfig(const std::string& config_path);
  explicit VioBackendConfig(const nlohmann::json& config_json);
  void ReadFromJson(const nlohmann::json& config_json);
  void WriteToJson(nlohmann::json* config_json) const;

  // Number of key camera frames in the initial sliding window.
  uint32_t initial_window_size = 1;
  // Number of key camera frames in other sliding windows.
  uint32_t sliding_window_size = 11;
  // Magnitude of gravity.
  float gravity = 9.8;
  // Projection error weight.
  ProjectionNoise projection_noise;
  // accelerometer measurement noise standard deviation.
  float accel_noise_density = -1.0;
  // gyroscope measurement noise standard deviation.
  float gyro_noise_density = -1.0;
  // accelerometer bias random work noise standard deviation.
  float accel_random_walk = -1.0;
  // gyroscope bias random work noise standard deviation.
  float gyro_random_walk = -1.0;
  // Max depth (meters) for triangulated points. Tune per scenario:
  // indoor ~20-30m, outdoor ~100-200m.
  float max_point_depth = 50.0f;
  // The max features count in msckf update.
  uint32_t max_features_in_update = 40;
  // The max features count of landmarks.
  uint32_t max_slam_features = 50;
  uint32_t max_slam_features_in_update = 25;
  // Covariance for raw pixel measurements
  float sigma_pix_sq = 1.0;
  // What chi-squared multipler we should apply
  float chi2_multiplier = 1.0;
  // Delayed time to add slam feature to system
  float slam_delayed_time = 2.0;
  // Representation of msckf landmarks
  LandmarkMeta::Representation msckf_representation =
      LandmarkMeta::Representation::ANCHORED_MSCKF_INVERSE_DEPTH;
  // Representation of slam landmarks
  LandmarkMeta::Representation slam_representation =
      LandmarkMeta::Representation::ANCHORED_MSCKF_INVERSE_DEPTH;
  // Use observability constraint in msckf
  bool use_oc = true;
  // Add slam features into the state and propagate the related cov after dynamic initialization.
  bool use_dynamic_initial_slam_features = false;
  // Use zero velocity update in msckf
  bool use_zupt = true;
  // The minimum feature count for parallax calculation
  uint32_t zupt_parallax_count_threshold = 20;
  // Threshold of parallax in zero velocity detection
  float zupt_parallax_threshold = 1.0;
  // Threshold of acceleration variance in zero velocity detection
  float zupt_acc_var_threshold = 0.02;
  // We do not use zupt if there are some slam points.
  uint32_t zupt_slam_points_threshold = 20;
  // Calibrate the camera intrinsics online or not.
  bool calib_camera_intrinsics = false;
  // Calibrate the camera extrinsics online or not.
  bool calib_camera_extrinsics = false;
  // Calibrate the offset between camera and imu online or not.
  bool calib_camera_time_delay = false;
  // The time delay between camera and imu. t_imu = t_cam + delay.
  float camera_time_delay = 0.0;
  // Rolling line delay, unit:s.
  float camera_line_delay = 0.0;
  // Calibrate line delay for rolling shutter.
  bool calib_rolling_shutter = false;
  // Processing interval in backend, unit:s.
  float image_interval_max = 0.5;
  // If the predicted period is less than the threshold, the confidence of pose
  // is 1.0. If the predicted period is bigger than 2 * threshold, the
  // confidence of pose is less than 0.5 (e.g.0.49), which means the pose cannot
  // be trusted. If the predicted period is between threshold and 2 * threshold,
  // the confidence is between 0.5 and 1.0.
  float pose_predict_period_threshold = 0.1;
  // Open local mapping function or not.
  bool enable_local_mapping = false;
  // Path of bag of word file
  std::string lba_voc_path = "";
  // Open loop closure function or not.
  bool enable_loop_closure = false;
  // Open realtime scene mesh function or not.
  bool enable_scene_mesh = false;
  // Some threshold for tracking lost judgment.
  TrackingLostConfig tracking_lost_config;
  // Config for which initialization methods.
  InitializerConfig initializer_config;
  // Optional relocalizer config, for relocalization after tracking lost.
  std::optional<RelocalizerWithMapConfig> relocalizer_config;
};

}  // namespace vio
}  // namespace dm

#endif  // VIO_BACKEND_BACKEND_CONFIG_H_
