// Copyright 2021 DeepMirror Inc. All rights reserved.

#ifndef VIO_FRONTEND_FAST_OPTICAL_FLOW_FRONT_END_H_
#define VIO_FRONTEND_FAST_OPTICAL_FLOW_FRONT_END_H_

#include <map>
#include <memory>
#include <set>
#include <unordered_map>
#include <utility>
#include <vector>

#include "common/image_undistortion.h"
#include "common/timer.h"
#include "math/transformation_utils.h"
#include "vio/data_types.h"
#include "vio/frontend/frontend_config.h"
#include "vio/frontend/zmssd_matcher.h"

namespace dm {
namespace vio {

class FastOpticalFlowFrontEnd {
 public:
  FastOpticalFlowFrontEnd() = delete;
  FastOpticalFlowFrontEnd(const dm::vio::VioFrontendConfig& config,
                          const std::vector<dm::common::ImageUndistortion>& image_undistortions,
                          const std::vector<dm::math::Rigid3d>& poses_camera_in_imu);
  ~FastOpticalFlowFrontEnd();

#ifndef __EMSCRIPTEN__
  // Add image from 0-th camera. Mostly used for single camera case.
  // Returns true if the frame is detected as a keyframe.
  bool AddImage(const cv_mini::Mat& image);

  // Add image from camera with id of camera_id.
  // Returns true if the frame is to be sent to the backend.
  bool AddImage(const uint32_t camera_id, const cv_mini::Mat& image);

  // The monocular case is also a special independent case, could use the same process.
  void AddImageIndependent(const uint32_t camera_id, const cv_mini::Mat& image);
  void AddImageStereo(const uint32_t camera_id, const cv_mini::Mat& image);
  void AddImageStereoPlusIndependent(const uint32_t camera_id, const cv_mini::Mat& image);
#endif  // __EMSCRIPTEN__

  // The interface for wasm.
  bool AddImage(uintptr_t ptr, bool drawing);
  std::vector<double> GetTimeCosts();

  // Get the VIO frame that will be passed to KeyFrameStateEstimator. This function only adds the
  // point observations, user of this class should add other info (timestamp, imu measurements)
  // before passing the frame to a KeyFrameStateEstimator.
  // Return true if the observations are the last visual observations with the same timestamp.
  void GetLatestVioFrame(dm::vio::VioProblemFrame* frame);

  const std::vector<std::vector<uint32_t>>& point_ids() const { return point_ids_; }
  std::vector<std::vector<cv_mini::Point2f>> point_coordinates();

  const std::set<uint32_t>& master_camera_ids() { return master_camera_ids_; }
  const std::set<uint32_t>& paired_cameras() { return paired_cameras_; }
  const std::unordered_map<uint32_t, bool>& last_is_processed() { return last_is_processed_; }
  const SensorSetType& sensor_set_type() { return sensor_set_type_; }
  void SetStatus(bool backend_is_initialized) { backend_is_initialized_ = backend_is_initialized; }
  void SetRotationPrior(const uint32_t camera_id,
                        const Eigen::Matrix3d& rotation_cur_from_last_camera);
  void ClearRotationPrior(const uint32_t camera_id);

  void SetDebugVisualization(bool enable) { debug_visualization_enabled_ = enable; }

 private:
  bool InBorder(const cv_mini::Point2f& pt, const uint32_t borderSize = 1U) const;
  template <typename T>
  void CombineVector(const std::vector<T>& src_v1, const std::vector<T>& src_v2,
                     std::vector<T>* dst_v);
  int FindIdIndex(const std::vector<uint32_t>& ids, const uint32_t& id_to_be_found) const;
  // We generate the mask by track length in descending order.
  void GenerateMask(const uint32_t& cam_id, std::vector<cv_mini::Point2f>* pts,
                    std::vector<uint32_t>* ids, std::vector<uint32_t>* lengths);
  double CalcDistance(const cv_mini::Point2f& pt1, const cv_mini::Point2f& pt2) const;
  void BuildPyramid(const uint32_t& cam_id, const cv_mini::Mat& img,
                    std::vector<cv_mini::Mat>* pyramids);
  void DetectFastFeatures(const uint32_t& cam_id, const cv_mini::Mat& img,
                          std::vector<cv_mini::Point2f>* cur_pts);
  // If a camera is slave, we do the tracking from its master camera.
  void DoTrackingFromMasterWithKLT(const uint32_t& slave_cam_id,
                                   const std::vector<cv_mini::Mat>& cur_pyramid);
  void DoTrackingFromMasterWithFastKLT(const uint32_t& slave_cam_id,
                                       const std::vector<cv_mini::Mat>& cur_pyramid);
  void DoTrackingFromMasterWithZMSSD(const uint32_t& slave_cam_id,
                                     const std::vector<cv_mini::Mat>& cur_pyramid);
  // Do the tracking from the last frame to the current frame for one camera.
  void DoTrackingFromLastFrame(const uint32_t& cam_id, const std::vector<cv_mini::Mat>& cur_pyramid,
                               std::vector<cv_mini::Point2f>* cur_pts);

  // Judge if the current frame has good tracking from the last frame.
  bool IsGoodTracking(const uint32_t cam_id);
  void AddNewMaster(const uint32_t camera_id);
  void AddNewMasterForMultiCam(const uint32_t camera_id);
  void RemoveOldMaster(const uint32_t camera_id);

#ifdef __DSP_FAST_CV__
  int InitQ6();
  void DeinitQ6();
#endif

  // Monocular, Stereo, Independent (binocular or more), Stereo+Independent (four cams).
  SensorSetType sensor_set_type_;
  // The config contains the parameters about extracting features, tracking features and
  // processing image.
  const dm::vio::VioFrontendConfig kConfig;
  const std::vector<dm::math::Rigid3d> kCameraExtrinsics;
  // Lift points to rays or project rays to points with undistortion.
  std::vector<dm::common::ImageUndistortion> image_undistortions_;
  // E21
  std::map<uint32_t, Eigen::Matrix3d> essential_matrix_;
  // One-shot rotation prior from the last image to the current image for each camera.
  std::map<uint32_t, Eigen::Matrix3d> rotation_prior_from_last_;
  // How many frames to skip before sending to backend.
  uint32_t frame_interval_;
  // Which camera is the master camera paired up with the current camera.
  std::unordered_map<uint32_t, uint32_t> paired_master_camera_;
  std::set<uint32_t> master_camera_ids_;
  uint32_t latest_point_id_;
  // We drop the first image for stable initialization with more imu measurements.
  uint32_t latest_frame_id_;
  uint32_t last_camera_id_;
  // Point ids and point coordinates from the last gray level image for each camera.
  std::vector<std::vector<uint32_t>> point_ids_;
  std::vector<std::vector<cv_mini::Point2f>> point_coordinates_;
  // We get the temporary points and their ids and lengths which tracked from the master camera for
  // the slave camera, and they will be merged with that tracked from the last slave camera later.
  std::vector<cv_mini::Point2f> temporary_pts_tracked_from_master_;
  std::vector<uint32_t> temporary_ids_tracked_from_master_;
  std::vector<uint32_t> temporary_lengths_tracked_from_master_;
  // Optical flow pyramid constructed from the last frame for each camera.
  std::unordered_map<uint32_t, std::vector<cv_mini::Mat>> last_pyramid_;
  // The masks help to keep the points evenly distributed.
  std::unordered_map<uint32_t, cv_mini::Mat> mask_;
  uint32_t image_height_;
  uint32_t image_width_;
  // The tracking lengths of points, and we will generate the mask by the tracking lengths in
  // descending order.
  std::unordered_map<uint32_t, std::vector<uint32_t>> tracking_lengths_;

  dm::common::Timer::Ptr tEH_;
  dm::common::Timer::Ptr tBP_;
  dm::common::Timer::Ptr tDFF_;
  dm::common::Timer::Ptr tFKLT_;
  dm::common::Timer::Ptr tBKLT_;
  dm::common::Timer::Ptr tERANSAC_;
  dm::common::Timer::Ptr tTL_;
  dm::common::Timer::Ptr tSKLT_;
  dm::common::Timer::Ptr tSZMSSD_;
  dm::common::Timer::Ptr tTotal_;

  bool backend_is_initialized_ = false;
  // The master camera can be only generated in the paired_cams (id_pairs).
  std::set<uint32_t> paired_cameras_;
  // The count of the slave camera tracked from the last frame, is used to skip.
  // The size of this map is equal to the size of total cameras.
  std::unordered_map<uint32_t, uint32_t> slave_tracking_cnt_from_last_;
  // The count of the slave camera tracked from the master frame, is used to skip.
  // The size of this map is equal to the size of id_pairs.
  std::unordered_map<uint32_t, uint32_t> slave_tracking_cnt_from_master_;
  // Will be true if do tracking for cur frame. For drawing images out of here.
  std::unordered_map<uint32_t, bool> last_is_processed_;
  // The count of IsGoodTracking(), to switch between master and slave cameras.
  std::unordered_map<uint32_t, uint32_t> continuous_good_tracking_length_;
  // The count of !IsGoodTracking(), to switch between master and slave cameras.
  std::unordered_map<uint32_t, uint32_t> continuous_bad_tracking_length_;
  uint32_t tracking_length_threshold_ = 3;

#ifdef __DSP_FAST_CV__
  uint32_t *fast_corners_ = NULL;
  uint32_t *fast_scores_ = NULL;
  uint32_t *tempBuf_ = NULL;
  uint32_t detected_corners_cnt_ = 0;  // DSP handle
  float *klt_feature_in_ = NULL;
  float *klt_feature_out_ = NULL;
  int *klt_feature_status_ = NULL;
  float *klt_feature_reverse_out_ = NULL;
  int *klt_feature_reverse_status_ = NULL;
#else
  cv_mini::Ptr<cv_mini::FastFeatureDetector> fast_detector_;
#endif

  ZMSSDMatcher zmssd_matcher_;

  double image_scale_down_factor_x_ = 1.0;
  double image_scale_down_factor_y_ = 1.0;

  bool debug_visualization_enabled_ = false;
};

}  // namespace vio
}  // namespace dm

#endif  // VIO_FRONTEND_FAST_OPTICAL_FLOW_FRONT_END_H_
