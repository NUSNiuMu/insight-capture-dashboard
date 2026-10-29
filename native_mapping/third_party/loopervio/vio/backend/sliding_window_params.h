// Copyright 2020 DeepMirror Inc. All rights reserved.

#ifndef VIO_BACKEND_SLIDING_WINDOW_PARAMS_H_
#define VIO_BACKEND_SLIDING_WINDOW_PARAMS_H_

#include <Eigen/Dense>
#include <algorithm>
#include <deque>
#include <functional>
#include <map>
#include <memory>
#include <random>
#include <set>
#include <unordered_map>
#include <unordered_set>
#include <string>
#include <utility>
#include <vector>
#include "vio/backend/frame_state.h"
#include "vio/imu_model.h"
#include "vio/ekf_filter/srif_filter.h"
#include "vio/ekf_filter/ekf_filter.h"

#define ENABLE_OPENCV 0
#if ENABLE_OPENCV
#define MINI2CV(mat) (cv::Mat(mat.rows, mat.cols, mat.type(), mat.data, mat.step))
#endif

#ifdef __x86_64__
#define VIO_DEBUG_MODE 1
#else
#define VIO_DEBUG_MODE 0
#endif

#define FILTERING_MODE 1
#define DYNAMIC_INITIALIZATION_MODE 1

#include "common/image_undistortion.h"
#include "vio/data_types.h"
#include "vio/backend/imu_preintegration.h"

namespace dm {
namespace vio {

enum MotionStatus : int {
  MOTION_UNCERTAIN = 0,
  MOTION_STATIC = 1,
  MOTION_DYNAMIC = 2,
};



// Data structure to store all the parameters that are to be optimized in a VIO sliding window.
// New parameters are added and outdated parameters are deleted as the sliding window moves forward.
class SlidingWindowParams {
 public:
  SlidingWindowParams();
  explicit SlidingWindowParams(double gravity_norm);
  ~SlidingWindowParams();

  // Push back one frame.
  void PushFrame(const dm::vio::MultiVioProblemFrame& multi_vio_frame,
                 dm::vio::ImuPreIntegration::Ptr pre_integration = nullptr);
  void SetCameraCount(const uint32_t& cam_cnt);
  bool VisualObservationsAreReady() const;
  bool HasLongTracking(const uint32_t point_id, int thres = 4) const;
  uint32_t num_frames() const;
  // Retrieves frame index by timestamp, returns -1 if timestamp is not found.
  int32_t FrameIndexByTimestamp(const uint64_t timestamp);
  FrameMeta::Ptr FrameByTimestamp(const uint64_t timestamp);
  FrameMeta::Ptr FrameByIndex(int index);
  std::map<uint32_t, PointMeta::Ptr>::const_iterator points_begin() const;
  std::map<uint32_t, PointMeta::Ptr>::const_iterator points_end() const;
  const std::map<uint32_t, PointMeta::Ptr>& points() const { return points_; }

  size_t point_num_observations(const uint32_t point_id) const;

  const std::map<ImageId, ImgCoord>& point_observations(uint32_t point_id) const {
    return points_.at(point_id)->observations;
  }

  void InitCameraParameters(const std::vector<dm::common::ImageUndistortion>& cam_intrinsics,
                            const std::vector<dm::math::Rigid3d>& cam_extrinsics) {
    cam_intrinsics_ = cam_intrinsics;
    cam_extrinsics_ = cam_extrinsics;
  }

  void set_max_point_depth(float d) { max_point_depth_ = d; }

  const dm::math::Rigid3d& cam_extrinsic(size_t idx) const { return cam_extrinsics_[idx]; }

  const dm::common::ImageUndistortion& cam_intrinsic(size_t idx) const {
    return cam_intrinsics_.at(idx);
  }

  const std::vector<dm::common::ImageUndistortion>& cam_intrinsics() const {
    return cam_intrinsics_;
  }

  const std::vector<dm::math::Rigid3d>& cam_extrinsics() const { return cam_extrinsics_; }

  size_t cam_size() const { return cam_extrinsics_.size(); }

  void set_sliding_window_size(size_t s) { sw_size_ = s; }
  // Note that the argument for the following functions is the frame index, not the frame
  // timestamp.
  uint64_t frame_timestamp(const uint32_t index) const;
  dm::vio::ImuPreIntegration* pre_integration(const uint32_t index) const;
  const Eigen::Vector3d& gravity() const;
  void set_gravity(const Eigen::Vector3d& gravity);
  IMUMeta::Ptr imu_meta() { return imu_meta_; }

  IMUMeta::Ptr previous_imu() { return previous_imu_; }

  void MapPointsToFrame(uint32_t frame_index);
  PointMeta::Ptr Point(uint32_t idx);
  FrameMeta::Ptr GetCurrentFrame();

  // Remove the old frames and visual measurements when the initialization is done.
  void RemoveOldInformation(const uint64_t& timestamp, bool closed = false);
  // Remove the record of the frame(will be deleted) from PointMeta
  void RemoveOldInfoFromPoints(const uint64_t& timestamp, bool closed = false);

  void PopFrame(const bool& use_jacobian = false);

  Eigen::Vector6d GetFeatureCorrespondPose(std::pair<ImageId, ImgCoord> observation);

  // Initialize a point's position from anchor to world based on frame pose and observations.
  bool InitializePoint(const std::vector<dm::math::Rigid3d>& camera_extrinsics, PointMeta::Ptr feat,
                       bool re_undistort = false);
  // Same but writes failure reason: "depth_bad", "cond_bad", or "" on success.
  bool InitializePoint(const std::vector<dm::math::Rigid3d>& camera_extrinsics, PointMeta::Ptr feat,
                       bool re_undistort, std::string* fail_reason);
  // Stereo-aware triangulation: tries stereo pair first, falls back to temporal DLT.
  bool InitializePointStereo(const std::vector<dm::math::Rigid3d>& camera_extrinsics,
                              PointMeta::Ptr feat, bool re_undistort = false,
                              std::string* fail_reason = nullptr);

  // Return the max size of covariance
  size_t GetCovarianceSize() const;

  MotionStatus GetSystemMotionStatus(const uint32_t& parallax_calculation_count_threshold,
                                     double parallax_static_threshold);
  void SetIMUNoise(const double& gyro_noise_density, const double& accel_noise_density,
                   const double& gyro_random_walk, const double& accel_random_walk);
  // Return the covariance of specified frames.
  Eigen::MatrixXf GetMarginalCovariance(const std::vector<BaseMeta::Ptr>& variables);

  Eigen::MatrixXd GetMarginalCovariance64(const std::vector<BaseMeta::Ptr>& variables);

  // Return the 6x6 pose covariance (rotation 3 + translation 3) of the latest frame.
  // Returns a zero matrix if not yet initialized or the frame has no valid state index.
  Eigen::Matrix<double, 6, 6> GetLatestFramePoseCov();

  // Return the features which are lost on the current frame.
  std::map<uint32_t, PointMeta::Ptr> GetLostFeatures(const uint64_t& current_timestamp);
  // Return the features which has measurement on the marg frame.
  std::map<uint32_t, PointMeta::Ptr> GetMargFeatures(const uint64_t& marginalization_timestamp,
                                                     bool check_initialized = true);

  // Calculate the transition matrix and imu covariance.
  void TransitionCalculation(const Eigen::Vector3d& accel, const Eigen::Vector3d& gyro,
                             const double& dt, Eigen::Matrix<double, 15, 15> *F,
                             Eigen::Matrix<double, 15, 15> *Q);
  // Predict the state by RK4.
  void RK4Prediction(const Eigen::Vector3d& accel_0, const Eigen::Vector3d& gyro_0,
                     const double& dt, const Eigen::Vector3d& accel_1,
                     const Eigen::Vector3d& gyro_1);

  // Propagate the state and the covariance of IMU.
  void IMUStatePropagation(const std::vector<Eigen::Vector3d>& accels,
                           const std::vector<Eigen::Vector3d>& gyros,
                           const std::vector<double>& dts, bool use_oc, bool is_zupt = false,
                           bool debug_log = false);

  void CopyImuMetaToFrame();

  // Return all id of slam features
  std::set<uint32_t> GetSLAMFeatureIds();

  // Return all slam features
  std::vector<std::pair<uint32_t, PointMeta::Ptr>> GetSLAMFeatures();

  void PushSLAMLandmark(const LandmarkMeta::Ptr& landmark);

  // Remove features from feature tracks
  void RemoveFeatures(const std::set<uint32_t>& feature_ids, bool check_to_delete);

  LandmarkMeta::Ptr GetLandmark(uint32_t id);

  std::vector<LandmarkMeta::Ptr> slam_landmarks() { return slam_landmarks_; }

  std::map<uint32_t, CameraParamsMeta::Ptr>* GetCameraParametersPtr(
      CameraParamsMeta::Representation rep) {
    if (rep == CameraParamsMeta::Representation::INTRINSICS) return &camera_intrinsics_;
    if (rep == CameraParamsMeta::Representation::EXTRINSICS) return &camera_extrinsics_;
    if (rep == CameraParamsMeta::Representation::TIME_DELAY) return &camera_time_delay_;
    return nullptr;
  }

  double gyro_noise_density_pow_2() { return gyro_noise_density_pow_2_; }
  double accel_noise_density_pow_2() { return accel_noise_density_pow_2_; }
  double gyro_random_walk_pow_2() { return gyro_random_walk_pow_2_; }
  double accel_random_walk_pow_2() { return accel_random_walk_pow_2_; }

  CameraParamsMeta::Ptr CameraIntrinsicMeta(const uint32_t cam_id);
  CameraParamsMeta::Ptr CameraExtrinsicMeta(const uint32_t cam_id);
  CameraParamsMeta::Ptr CameraTimeDelayMeta(const uint32_t cam_id);
  void SyncCameraIntrinsics();
  void SyncCameraExtrinsics();

  std::deque<FramePose> GetSlidingFramePoses();

  void ResetMSCKFFromRelocalization(
      const uint64_t& reloc_timestamp, const dm::math::Rigid3d& reloc_pose,
      const Eigen::Vector3d& reloc_velocity, const Eigen::Vector3d& accel_bias,
      const Eigen::Vector3d& gyro_bias,
      const std::map<CameraParamsMeta::Representation, std::vector<Eigen::VectorXd>>& camera_params,
      bool calib_camera_time_delay, const Eigen::Vector3d& angle_velocity);

  // Calculate the related jacobian and residuals for the specified feature
  void ComputeGlobalPtJacobian(const Eigen::Vector3d& wP, const FrameMeta::Ptr& anchor_frame,
                               uint32_t camera_id, Eigen::MatrixXf* Jpt_x,
                               Eigen::MatrixXf* Jpt_f, Eigen::MatrixXf* Jpt_Tic);

  void ComputeGlobalPtJacobian64(const Eigen::Vector3d& wP, const FrameMeta::Ptr& anchor_frame,
                               uint32_t camera_id, Eigen::MatrixXd* Jpt_x,
                               Eigen::MatrixXd* Jpt_f, Eigen::MatrixXd* Jpt_Tic);

#if DYNAMIC_INITIALIZATION_MODE
  // Initialize a point's position by two camera pose.
  Eigen::Vector3d InitializePoint(const dm::math::Rigid3d& pose0, const dm::math::Rigid3d& pose1,
                                  const Eigen::Vector2d& point0, const Eigen::Vector2d& point1);
#endif

  void ErasePoint(FeatureId feat_id) {
    if (points_.count(feat_id)) points_.erase(feat_id);
  }

  void RemoveNewFrame();

  Eigen::Vector3d TransformPointFromCameraToWorld(const Eigen::Vector3d& c_pt,
                                                  const FrameMeta::Ptr& anchor_frame,
                                                  uint32_t camera_id);
  Eigen::Vector3d TransformPointFromWorldToCamera(
      const Eigen::Vector3d& w_pt, const FrameMeta::Ptr& anchor_frame, uint32_t camera_id);

  void InitializeSRIF(const std::map<CameraParamsMeta::Representation,
                                     std::vector<Eigen::VectorXd>>& camera_params,
                      bool calib_time_delay = false);

  void AddSrifResidual(SrifResidualBlock::Ptr residual) {
    sri_filter_->AddResidualBlock(residual);
  }

  void InitializeSlamLandmarks(const std::vector<SlamFeatInitInfo::Ptr>& slam_infos);

  std::vector<LandmarkMeta::Ptr> PropagateLandmarks(const LandmarkMeta::Representation& rep,
                                                    int new_frm_idx = -2);

  void SrifSolve(bool solve_now = true, bool only_msckf = false) {
    sri_filter_->Solve(solve_now, only_msckf);
  }

  void SrifSolveSlamUpdate(bool solve_now = true) { sri_filter_->SolveSlamUpdate(solve_now); }

  SRIFilter::Ptr sri_filter() {
    return sri_filter_;
  }

  void SrifImuPropagation(const Eigen::MatrixXd& F, const Eigen::MatrixXd& Q, bool is_zupt = false);

  void SrifMarginOld(const std::vector<LandmarkMeta::Ptr>& marg_landmarks = {});

  void SrifMarginZupt(const FrameMeta::Ptr marg_frm,
                      const std::vector<LandmarkMeta::Ptr>& landmarks = {});

  void SrifAddPrior(bool use_big_prior = false) {
    use_big_prior ? sri_filter_->AddBigPrior() : sri_filter_->AddPrior();
  }

  void UpdateCovariance() {
#ifdef FAST_COV_IN_SRIF
    sri_filter_->FastUpdateCovariance();
#else
    sri_filter_->UpdateCovariance();
#endif
  }

  void AdjustCovariance() {
#ifdef SQUARE_ROOT_INVERSE
    if (!camera_time_delay_.empty()) {
      auto td_id = camera_time_delay_[0]->local_id();
      VLOG(2) << "covariance td: " << sri_filter_->covariance()(td_id, td_id);
      sri_filter_->AdjustSquareInfo(camera_time_delay_[0]);
    }
#endif
  }

  void InitializeMSCKF(
      const std::map<CameraParamsMeta::Representation, std::vector<Eigen::VectorXd>>& camera_params,
      bool calib_time_delay);

  void EKFAugmentFrame(bool calib_time_delay);

  void EKFUpdate(const std::vector<BaseMeta::Ptr>& Hx_order,
                 const Eigen::MatrixXd& H, const Eigen::VectorXd& res,
                 const Eigen::MatrixXd& R) {
    TIMER_DECORATOR(ek_filter_->EKFUpdate(Hx_order, H, res, R), EKFUpdate, 2);
  }

  void EKFInitializeInvertible(BaseMeta::Ptr new_var, const std::vector<BaseMeta::Ptr>& Hx_order,
                               const Eigen::MatrixXd& H_x,
                               const Eigen::MatrixXd& H_f,
                               const Eigen::VectorXd& res, const Eigen::MatrixXd& R) {
    return ek_filter_->InitializeInvertible(new_var, Hx_order, H_x, H_f, res, R);
  }

  void PerformAnchorChange(LandmarkMeta::Ptr landmark, uint64_t new_timestamp, uint32_t cam_id);

  // Remove the marg frame and related covariance.
  void MSCKFMarginalization();

  void MSCKFMarginalizationFrame(FrameMeta::Ptr marg_frm);

  void RemoveObservations(const Timestamp& timestamp);

  void SLAMMarginalization();

  bool IsNewKeyframe(float msckf_error, int msckf_feature_size, bool use_pos, bool use_parallax);

    EIGEN_MAKE_ALIGNED_OPERATOR_NEW

 public:
  // Injected once at construction time; see vio/imu_model.h.
  void SetImuModel(const ImuModel& m) { imu_model_ = m; }
  const ImuModel& imu_model() const { return imu_model_; }

 private:
  ImuModel imu_model_;
  // Stereo observation pair at the same timestamp (cam0 + cam1).
  struct StereoObservation {
    std::pair<ImageId, ImgCoord> obs0;
    std::pair<ImageId, ImgCoord> obs1;
  };
  // Returns the latest timestamp where both cam0 and cam1 have an observation for this feature.
  bool FindStereoPair(const PointMeta::Ptr& feat, StereoObservation* result);
  // Triangulate from a single stereo pair using the known baseline. Sets feat->position (world).
  bool StereoTriangulate(const std::vector<dm::math::Rigid3d>& camera_extrinsics,
                         const StereoObservation& stereo, PointMeta::Ptr feat, bool re_undistort);
  // Returns the maximum angular parallax (degrees) among same-camera temporal observations,
  // and writes the cam_id that achieves this maximum into best_cam_id (may be null).
  // Rays are evaluated in world frame, so pure rotation gives ~0° while translation gives >0°.
  double ComputeMaxTemporalParallax(const std::vector<dm::math::Rigid3d>& camera_extrinsics,
                                    const PointMeta::Ptr& feat, bool re_undistort,
                                    uint32_t* best_cam_id);
  // DLT triangulation using only observations from dlt_cam_id, with condition-number gate.
  // Sets feat->position on success. Does NOT set feat->is_initialized.
  bool TemporalDLTTriangulate(const std::vector<dm::math::Rigid3d>& camera_extrinsics,
                               PointMeta::Ptr feat, bool re_undistort, uint32_t dlt_cam_id,
                               std::string* fail_reason);
  // Checks whether feat->position has a physically plausible depth in the anchor camera frame.
  bool CheckPointDepth(const std::vector<dm::math::Rigid3d>& camera_extrinsics,
                       const PointMeta::Ptr& feat);
  // Gauss-Newton refinement of feat->position using all observations. Modifies feat->position.
  void GaussNewtonRefinePoint(const std::vector<dm::math::Rigid3d>& camera_extrinsics,
                               PointMeta::Ptr feat, bool re_undistort);

  // We use a map of point id to PointMeta to store the point parameters.
  std::map<uint32_t, PointMeta::Ptr> points_;
  std::vector<LandmarkMeta::Ptr> slam_landmarks_;

  std::deque<FrameMeta::Ptr> frames_;

  IMUMeta::Ptr imu_meta_ = nullptr;
  IMUMeta::Ptr previous_imu_ = nullptr;
  std::map<uint32_t, CameraParamsMeta::Ptr> camera_intrinsics_;
  std::map<uint32_t, CameraParamsMeta::Ptr> camera_extrinsics_;
  std::map<uint32_t, CameraParamsMeta::Ptr> camera_time_delay_;

  // The pointer which points to camera_intrinsics_ in KeyFrameStateEstimator.
  // We don't need to consider locks because the functions in SlidingWindowParams
  // will be called serially in KeyFrameStateEstimator.
  std::vector<dm::common::ImageUndistortion> cam_intrinsics_;
  std::vector<dm::math::Rigid3d> cam_extrinsics_;
  float max_point_depth_ = 30.0f;

  SRIFilter::Ptr sri_filter_;
  EKFilter::Ptr ek_filter_;

  // The cov of imu_meta and frames_, which size is 15+6*n+x

  double gyro_noise_density_pow_2_;
  double accel_noise_density_pow_2_;
  double gyro_random_walk_pow_2_;
  double accel_random_walk_pow_2_;

  // When we get all the visual frames with the same timestamp, we send them together to the
  // backend.
  uint32_t camera_count_;
  uint32_t visual_frame_with_same_timestamp_count_;
  Eigen::Vector3d gravity_;

  size_t sw_size_ = 11;

  // Latest frame timestamp seen per camera. Updated every PushFrame.
  // Used by GetLostFeatures to determine if a camera is still actively tracking a feature.
  std::map<uint32_t, uint64_t> last_cam_frame_ts_;

#if VIO_DEBUG_MODE
  bool debug_push_count_active_ = false;
  // Total push count per point (never cleared by RemoveFeatures). Only populated when
  // debug_push_count_active_ is true. If push count exceeds observations.size(), the point
  // was deleted from the backend while the frontend kept tracking it.
  std::unordered_map<uint32_t, uint32_t> debug_push_count_;
#endif

  friend class KeyFrameStateEstimator;
  friend class VisualImuAlignment;
};

}  // namespace vio
}  // namespace dm

#endif  // VIO_BACKEND_SLIDING_WINDOW_PARAMS_H_
