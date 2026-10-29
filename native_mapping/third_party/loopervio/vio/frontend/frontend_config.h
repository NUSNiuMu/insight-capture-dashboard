#ifndef VIO_FRONTEND_FRONTEND_CONFIG_H_
#define VIO_FRONTEND_FRONTEND_CONFIG_H_

#include <optional>
#include <string>
#include <vector>
#include "nlohmann/json.hpp"

namespace dm {
namespace vio {

struct IntPair {
  uint32_t master = 0;
  uint32_t slave = 1;
};

enum StereoMatchingType { STEREO_KLT = 0, STEREO_ZMSSD = 1 };

struct ImageResolutionToProcess {
  uint32_t width = 640;
  uint32_t height = 480;
};

struct VioFrontendConfig {
  VioFrontendConfig() = default;
  explicit VioFrontendConfig(const std::string& config_path);
  explicit VioFrontendConfig(const nlohmann::json& config_json);
  void ReadFromJson(const nlohmann::json& config_json);
  void WriteToJson(nlohmann::json* config_json) const;
  // Targeted number of fast features on one frames.
  uint32_t target_num_of_features = 150;
  // Threshold used to detect fast features.
  uint32_t fast_threshold = 15;
  // Minimum distance between features.
  uint32_t minimum_distance = 30;
  // The window size of image processing in building pyramid and KLT tracking.
  uint32_t window_size = 21;
  // The levels of pyramid which will be builded.
  uint32_t pyramid_levels = 3;
  // Equalize the histogram or not for image.
  bool do_equalization = false;
  // Do the forward-backward tracking check or not.
  bool do_reverse_check = true;
  // Maximum distance of the tracking result in reverse check.
  float max_reverse_check_distance = 0.5;
  // How many frames to skip before sending to backend.
  uint32_t frame_interval = 1;
  // number of cameras.
  uint32_t num_cameras = 2;
  // The camera with the first id is the master camera. We do the tracking from
  // the last frame for the master camera and do tracking from master frame to
  // slave frame for the slave camera. For example, we have three cameras with
  // ids {0, 1, 2} and the camera with id 0 is the master camera, so we have the
  // pairs {0, 1} and {0, 2}. There also can be more than one master camera. For
  // example, we have four cameras with ids {0, 1, 2, 3}, and the cameras with
  // id 0 and id 2 are the master cameras, and we have the pairs {0, 1} and {2, 3}.
  std::vector<IntPair> id_pairs;
  // Switch the master and slave or not when bad tracking for the master camera.
  bool switch_master_slave = false;
  // Maximum distance between the tracking result from the master camera and the
  // tracking result from the slave camera.
  float max_master_slave_tracking_distance = 1.0;
  // The mathod of stereo matching.
  StereoMatchingType stereo_matching_type = STEREO_KLT;
  // The epipolar threshold for matching between master and slave camera.
  float epipolar_threshold = 1.0;
  // STEREO_KLT seeds each slave match by projecting the master feature at this depth (meters),
  // then retries the unmatched ones at stereo_rescue_depths in order. KLT only converges from a
  // seed a few pixels off, so these must bracket the scene: the defaults suit 0.5-3 m, and a far
  // scene (a crane rig at 10-100 m) needs e.g. 20 m first, or far features lock onto a wrong
  // minimum along the epipolar line.
  float stereo_nominal_depth = 1.5f;
  std::vector<double> stereo_rescue_depths = {0.5, 3.0};
  // Enable temporal epipolar RANSAC rejection in single-camera tracking.
  bool enable_temporal_ransac = true;
  // Maximum time interval in seconds between two frontend-processed IMU poses
  // for using the integrated rotation as a visual tracking prior.
  float rotation_prior_max_interval = 0.1;
  // The interval of tracking for slave camera under two case. Note that
  // slave_tracking_interval_from_master must be a multiple of slave_tracking_interval_from_last.
  uint32_t slave_tracking_interval_from_last = 2;
  uint32_t slave_tracking_interval_from_master = 2;
  // The ratio threshold about detecting new corners.
  float detect_ratio_threshold = 1.0;
  // The downsampling resolution of cam for frontend processing.
  std::optional<ImageResolutionToProcess> image_resolution_to_process;
  // Fraction of image height to exclude from the bottom for feature detection (e.g. 0.2 = bottom
  // 20%). Useful when a gripper or other occluder is visible at the bottom of the image (e.g. UMI).
  float image_mask_bottom_ratio = 0.0f;
  // Radius, as a fraction of half the image height and measured from the image center, outside
  // which features are neither detected nor kept (0 = off). For fisheye lenses whose image-circle
  // rim carries reflections of bright lights that do not move with the scene.
  float image_mask_circle_radius_ratio = 0.0f;
};

struct OrbExtractorConfig {
  uint32_t features_num = 800;
  float pyramid_scale_factor = 1.2;
  uint32_t pyramid_levels = 8;
  uint32_t init_th_fast = 20;
  uint32_t min_th_fast = 7;
};

}  // namespace vio
}  // namespace dm

#endif  // VIO_FRONTEND_FRONTEND_CONFIG_H_
