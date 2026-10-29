// Copyright 2020 DeepMirror Inc. All rights reserved.

#ifndef VIO_BACKEND_KEY_FRAME_STATE_ESTIMATOR_H_
#define VIO_BACKEND_KEY_FRAME_STATE_ESTIMATOR_H_

#include <atomic>
#include <deque>
#include <map>
#include <memory>
#include <set>
#include <tuple>
#include <unordered_map>
#include <utility>
#include <vector>
#include <string>
#include "common/image_undistortion.h"
#include "common/timer.h"
#include "vio/backend/backend_config.h"
#include "vio/imu_model.h"
#include "vio/backend/frame_state.h"
#include "vio/initializer/base_initializer.h"
#include "vio/pose_maintainer.h"
#include "vio/backend/sliding_window_params.h"
#include "vio/vio_system_status.h"
#ifndef __EMSCRIPTEN__
#include "vio/map/local_mapping.h"
#include "vio/map/loop_closure.h"
#include "vio/thread/vio_thread.h"
#include "vio/recon/simple_plane_recon.h"
#endif

namespace dm {
namespace vio {

#ifdef __EMSCRIPTEN__
class KeyFrameStateEstimator {
#else
class KeyFrameStateEstimator : public VioThread {
#endif

 public:
  using Ptr = std::shared_ptr<KeyFrameStateEstimator>;

  KeyFrameStateEstimator(const VioBackendConfig& config,
                         const std::vector<dm::common::ImageUndistortion>& camera_intrinsics_,
                         const std::vector<dm::math::Rigid3d>& camera_extrinsics,
                         const SensorSetType& sensor_set_type);

  // Fan the IMU measurement model out to every component that turns (measurement, bias)
  // into a physical quantity: the sliding-window propagation, the pose predictor, and any
  // preintegration created from here on. See vio/imu_model.h.
  void SetImuModel(const ImuModel& m) {
    imu_model_ = m;
    params_.SetImuModel(m);
    pose_maintainer_.SetImuModel(m);
  }

  void AddImuMeasurement(const uint64_t timestamp, const Eigen::Vector3d& linear_acceleration,
                         const Eigen::Vector3d& angular_velocity, bool need_to_propagate = true);

  // For now a key frame is assumed to contain a full IMU measurement. This function calls
  // AddImuMeasurement, so there is no need to call AddImuMeasurement when a key frame arrives.
  void AddKeyFrame(const MultiVioProblemFrame& frame);

  void ComputeRollingIncPose(FrameMeta::Ptr frame);

  const dm::common::ImageUndistortion& camera_intrinsic(CameraId camera_id) const {
    return params_.cam_intrinsic(camera_id);
  }
  const dm::math::Rigid3d& camera_extrinsic(CameraId camera_id) const {
    return params_.cam_extrinsic(camera_id);
  }

  // On return, if *timestamp is equal to zero, it means this estimator is not initialized yet,
  // otherwise the timestamp and the state are valid.
  void GetLatestStateEstimate(uint64_t* timestamp, FrameState* state);

  // On return, if *timestamp is equal to zero, it means this estimator is not initialized yet,
  // otherwise the timestamp and the state at imu frequency are valid.
  void GetLatestIMUStateEstimate(uint64_t* timestamp, FrameState* state, bool is_custom = false,
                                 Eigen::Vector3d* linear_velocity = nullptr,
                                 Eigen::Vector3d* angular_velocity = nullptr,
                                 Eigen::Vector3d* linear_acceleration = nullptr);

  int GetFramePoseByTimestamp(uint64_t timestamp, math::Rigid3d* Twi);
  // Use the slam features to fit a plane, get centroid and normal vector.
  // Return false if no plane is detected.
  bool GetPlaneInCurView(Eigen::Vector3d* centroid, Eigen::Vector3d* normal);

  MotionStatus GetSystemMotionStatus();

  VioSystemStatus GetSystemStatus();

  inline bool system_initialized() { return IsTracking(system_status_); }

  void set_need_to_collect_landmarks(bool state) { need_to_collect_landmarks_ = state; }

  dm::common::ImageUndistortion camera_intrinsic(CameraId camera_id) {
    return params_.cam_intrinsic(camera_id);
  }

  dm::math::Rigid3d camera_extrinsic(CameraId camera_id) {
    return params_.cam_extrinsic(camera_id);
  }
  Eigen::Matrix3d GetInitR() { return initR_; }

  std::vector<double> GetTimeCosts();
  std::vector<double> GetCamParams(uint32_t cam_id);
  std::set<uint32_t> GetLandMarkIds();
  std::set<uint32_t> GetPlaneInlierIds() { return plane_inlier_ids_; }

  std::vector<Point3d> vio_point3ds() {
    std::lock_guard<std::mutex> lg(point3ds_mutex_);
    return vio_point3ds_;
  }

  std::map<Timestamp, dm::math::Rigid3d> vio_kf_poses() {
    std::lock_guard<std::mutex> lg(vio_kf_poses_mutex_);
    return vio_kf_poses_;
  }
#ifndef __EMSCRIPTEN__
  lm::Map* map() const {
    if (local_mapping_)
      return local_mapping_->map();
    else
      return nullptr;
  }

  lm::LocalMapping::Ptr local_mapping() const { return local_mapping_; }
  lm::LoopClosure::Ptr loop_closure() const { return loop_closure_; }
  void StopLocalMapping();

  dm::recon::Mesh GetPlaneMesh() {
    if (simp_plane_recon_) return simp_plane_recon_->GetMesh();
    return dm::recon::Mesh();
  }
#endif
  TrackingInfo tracking_info() { return tracking_info_; }

  uint32_t GetPoseFrequencyImu() { return pose_maintainer_.GetPoseFrequencyImu(); }
  uint32_t GetPoseFrequencyCam() { return pose_maintainer_.GetPoseFrequencyCam(); }
  double GetPredictPeriodBasedOnImu() { return pose_maintainer_.GetPredictPeriodBasedOnImu(); }
  float GetBackendTimecost() { return static_cast<float>(tBE_->Avg()); }
  float GetCamPoseDelay() { return cam_pose_delay_avg_; }

  // Returns the 6x6 pose covariance (rotation 3 + translation 3) of the latest image frame,
  // updated after each Filtering(). raw_timestamp carries the original camera frame timestamp
  // so callers can detect whether the covariance has been refreshed since the last query.
  void GetLatestFramePoseCovariance(uint64_t* raw_timestamp,
                                    Eigen::Matrix<double, 6, 6>* cov) const {
    std::lock_guard<std::mutex> lg(frame_pose_cov_mutex_);
    *raw_timestamp = latest_frame_pose_cov_timestamp_;
    *cov = latest_frame_pose_cov_;
  }

  uint64_t latest_processed_raw_timestamp() const {
    return latest_processed_raw_timestamp_.load(std::memory_order_acquire);
  }

  // Enable IMU state logging (before/after MSCKF update) for frames after abs_ns.
  void SetDebugStartTimeNs(uint64_t abs_ns) { debug_start_time_ns_ = abs_ns; }

  EIGEN_MAKE_ALIGNED_OPERATOR_NEW

 private:
#ifndef __EMSCRIPTEN__
  virtual void Run();
  virtual void Flush() {}
#endif
  // Start to estimate the pose in the backend.
  void DoEstimation(const dm::vio::MultiVioProblemFrame& frame);
  // Return n+1 accels, n+1 gyros and n delta_t, use to mid-integrate.
  ImuPack GetImuMeasurement(const uint64_t& start_time, const uint64_t& end_time);

  // Create 3d slam landmark from a feature track
  LandmarkMeta::Ptr CreateLandmark(const PointMeta::Ptr& feature,
                                   const LandmarkMeta::Representation& rep);

  // Multi-state constraint kalman filter
  void Filtering();

  // Calculate the related jacobian and residuals for the specified feature
  void JacobianAndResidualCalculation(const PointMeta::Ptr& feature,
                                      const LandmarkMeta::Representation& feat_representation,
                                      Eigen::MatrixXf* H_f, Eigen::MatrixXf* H_x,
                                      Eigen::VectorXf* res, std::vector<BaseMeta::Ptr>* x_order,
                                      bool online_params_opt = true);

  void JacobianAndResidualCalculation64(const PointMeta::Ptr& feature,
                                      const LandmarkMeta::Representation& feat_representation,
                                      Eigen::MatrixXd* H_f, Eigen::MatrixXd* H_x,
                                      Eigen::VectorXd* res, std::vector<BaseMeta::Ptr>* x_order);

  void MSCKFUpdateCore(std::vector<std::pair<uint32_t, PointMeta::Ptr>> *msckf_features);

  void MSCKFUpdateCoreEKF(std::vector<std::pair<uint32_t, PointMeta::Ptr>> *msckf_features);

  void MSCKFUpdate(std::vector<std::pair<uint32_t, PointMeta::Ptr>>* msckf_features,
                   bool solve_now);

  void MSCKFAndSLAMFeatureSelection(
      const std::set<uint32_t>& slam_feature_ids, int slam_feat_size,
      std::vector<std::pair<uint32_t, PointMeta::Ptr>>* msckf_features,
      std::vector<std::pair<uint32_t, PointMeta::Ptr>>* slam_features,
      std::set<FeatureId>* marg_only_ids, std::set<FeatureId>* drop_ids);

  // Select features to used as slam feature from candidates
  std::map<uint32_t, PointMeta::Ptr> SelectSLAMFeatures(
      int max_size, std::map<uint32_t, PointMeta::Ptr>* candidates);

  // Delayed initialize slam landmarks

  void SLAMDelayedInit(std::vector<std::pair<uint32_t, PointMeta::Ptr>>* slam_features);

  int SLAMDelayedInitCore(std::vector<std::pair<uint32_t, PointMeta::Ptr>>* slam_features);

  int SLAMDelayedInitCoreEKF(std::vector<std::pair<uint32_t, PointMeta::Ptr>>* slam_features);

  // Dump the chi2 innovation covariance and the filter's IMU/frame covariance blocks when a
  // delayed-init chi2 test fails. Debug only, gated by the caller on debug_start_time_ns_.
  void LogChi2FailCovariance(const Eigen::MatrixXf& S);

  void SLAMUpdate(std::vector<std::pair<uint32_t, PointMeta::Ptr>>* slam_features);

  // Update slam landmarks and other system states
  void SLAMUpdateCore(std::vector<std::pair<uint32_t, PointMeta::Ptr>>* slam_features);

  void SLAMUpdateCoreEKF(std::vector<std::pair<uint32_t, PointMeta::Ptr>>* slam_features);

  void SyncCameraIntrinsicsToOtherModules();

  // Sync information (such as position, anchor camera id and anchor frame time) from landmark to
  // feature track
  void SyncLandmarkToFeature(const LandmarkMeta::Ptr& landmark, PointMeta::Ptr feature);

  // zero velocity detection
  float ComputeAccVar(const std::vector<Eigen::Vector3d>& accs);

  bool ZeroVelocityCheck();

  // use q_{k|k-1}==q_{k-1|k-1}, p_{k|k-1}==p_{k-1|k-1}and v_{k|k-1}==0 as observation to update imu
  // state
  void ZeroVelocityUpdateQPV(const ImuPack& imu_pack);

  void ZeroVelocityUpdateQPVEKF(const ImuPack& imu_pack);

  bool TryZuptUpdate(const ImuPack& imu_pack);

  void RemoveObservations(const Timestamp& timestamp);

  void ZUPTRemoveImuMeasurement(const Timestamp& start_time, const Timestamp& end_time);

  void ChangeSLAMLandmarkAnchor();

  Eigen::Vector3d GetWorldPosition(const LandmarkMeta::Ptr& landmark);

  bool IsTrackingLost();

  Eigen::Vector3d GetAngleVelocityAtSpecifiedTimestamp(const uint64_t& specified_timestamp);
  void ResetMSCKFFromRelocalization(uint64_t reloc_timestamp, const dm::math::Rigid3d& reloc_pose,
                                    const Eigen::Vector3d& reloc_velocity);

  void PrintLatestState();

  bool Initialize();

  void InitializeFilter(bool add_slam_pt);

  bool WaitForMoreFrame(size_t frm_size = 5);

  using PointMetaPack = std::vector<std::pair<uint32_t, PointMeta::Ptr>>;
  void FeatureSelection(PointMetaPack *msckf_feats4update,
                        PointMetaPack *slam_feats4update,
                        PointMetaPack *slam_feats4init,
                        std::set<FeatureId>* marg_only_ids,
                        std::set<FeatureId>* drop_ids);

  std::set<FeatureId> FeatureIds(const PointMetaPack& point_meta_pack);

  void CollectVioResult(const PointMetaPack& msckf_feats4update,
                        const PointMetaPack& slam_feats4update,
                        const PointMetaPack& slam_feats4init);


#if VIO_DEBUG_MODE
  float ComputeReprojError(const PointMeta::Ptr& point, bool re_triangulate);
  std::tuple<size_t, float> ComputeReprojErrors(
      const std::vector<std::pair<uint32_t, PointMeta::Ptr>>& points, bool re_triangulate);
#endif

  dm::common::Timer::Ptr tPA_;
  dm::common::Timer::Ptr tMU_;
  dm::common::Timer::Ptr tSU_;
  dm::common::Timer::Ptr tSDI_;
  dm::common::Timer::Ptr tBE_;

#if DYNAMIC_INITIALIZATION_MODE
  // Add slam features into the state and propagate the related cov after dynamic initialization
  void AddSLAMFeatures();
#endif  // DYNAMIC_INITIALIZATION_MODE

  // Functions for debug
#ifndef __EMSCRIPTEN__
  cv_mini::Mat DrawMatches(FrameMeta::Ptr frm0,
                           FrameMeta::Ptr frm1,
                           CameraId cam_id0,
                           CameraId cam_id1);

  cv_mini::Mat DrawPointsInFrame(const std::vector<vio::Point3d>& pt3ds,
                                 FrameMeta::Ptr frm, CameraId cam_id);
#endif

  void ComputeAngleChange(std::string tag) {
    auto imu_meta = params_.imu_meta();
    if (!imu_meta) return;
    auto mat0 = dm::math::AngleVectorToRotation(imu_meta->orientation_fej).matrix();
    Eigen::Vector3d r0 = dm::math::Rotation2YawPitchRoll(mat0);
    auto mat1 = dm::math::AngleVectorToRotation(imu_meta->pose.head(3)).matrix();
    Eigen::Vector3d r1 = dm::math::Rotation2YawPitchRoll(mat1);
    VLOG(2) << "MSCKF CHANGE ERROR IMU: " << tag << " " << (r0 - r1).transpose();

    auto frm = params_.FrameByIndex(0);
    if (!frm) return;
    auto fmat0 = dm::math::AngleVectorToRotation(frm->last_pose.head(3)).matrix();
    Eigen::Vector3d fr0 = dm::math::Rotation2YawPitchRoll(fmat0);
    auto fmat1 = frm->Rot().matrix();
    Eigen::Vector3d fr1 = dm::math::Rotation2YawPitchRoll(fmat1);
    VLOG(2) << "MSCKF CHANGE ERROR Frame0: " << tag << " " << (fr0 - fr1).transpose();
  }

  float ComputeMsckfResidual(const std::vector<std::pair<uint32_t,
                                                         PointMeta::Ptr> >& msckf_features);

  VioBackendConfig config_;
  ImuModel imu_model_;
  SlidingWindowParams params_;
#ifndef __EMSCRIPTEN__
  lm::LocalMapping::Ptr local_mapping_ = nullptr;
  lm::LoopClosure::Ptr loop_closure_ = nullptr;
  recon::SimpPlaneRecon::Ptr simp_plane_recon_ = nullptr;
#endif  // __EMSCRIPTEN__

  // If the camera time delay decreased more than one imu cycle, we should skip the imu size check
  // during tracking. This is to avoid the size check failure when the time interval between frames
  // might not contain enough imu measurements simply because the timestamp is adjusted too
  // aggressively.
  double skip_imu_size_check_due_to_delay_jump_ = false;
  double camera_time_delay_;  // seconds
  double camera_line_delay_;
  bool calib_rolling_shutter_;

  std::atomic<VioSystemStatus> system_status_{NOT_INITED};
  MotionStatus motion_status_ = MOTION_UNCERTAIN;
  uint64_t initialize_timestamp_;

  Eigen::Matrix3d initR_;

  uint32_t non_continuous_visual_constraint_times_ = 0;

  // Deque of Image measurements, to save frames that have not enough imu measurements.
  std::deque<dm::vio::MultiVioProblemFrame> vio_frame_measurements_;

  // Deque of IMU measurements, measurement timestamps are in increasing order.
  std::deque<std::tuple<Timestamp, Eigen::Vector3d, Eigen::Vector3d>> imu_measurements_;
  std::mutex measurements_mutex_;
  PoseMaintainer pose_maintainer_;

  // The timestamp and state at cam frequency
  Timestamp latest_result_timestamp_ = 0;
  FrameState latest_frame_state_;

  // Latest image frame pose covariance, updated at the end of each Filtering().
  mutable std::mutex frame_pose_cov_mutex_;
  uint64_t latest_frame_pose_cov_timestamp_ = 0;
  Eigen::Matrix<double, 6, 6> latest_frame_pose_cov_ = Eigen::Matrix<double, 6, 6>::Zero();
  std::atomic<uint64_t> latest_processed_raw_timestamp_{0};

  bool processed_cur_frame_ = true;
  Timestamp cur_timestamp_ = 0;
  Timestamp last_frm_timestamp_ = 0;
  FramePose last_tracking_pose_;

  // Used to pass data to loop closure
  std::vector<std::pair<uint32_t, PointMeta::Ptr>> msckf_features_for_updating_;
  bool need_to_collect_loop_data_ = false;
  bool need_to_collect_landmarks_ = false;
  std::mutex latest_info_mutex_;
  FrameMeta::Ptr latest_frame_ = nullptr;
  std::set<uint32_t> plane_inlier_ids_;

  BaseInitializer::Ptr static_initializer_ = nullptr;

  BaseInitializer::Ptr dynamic_initializer_ = nullptr;

  bool zupt_static_ = false;

  PoseWriter::Ptr pose_writer_ = nullptr;

  Timestamp last_vio_frame_timestamp_ = 0;

  std::mutex point3ds_mutex_;
  std::vector<vio::Point3d> vio_point3ds_;
  std::mutex vio_kf_poses_mutex_;
  std::map<Timestamp, dm::math::Rigid3d> vio_kf_poses_;

  // Monocular, Stereo, Independent (binocular or more), Stereo+Independent (four cams).
  SensorSetType sensor_set_type_;
  uint32_t recent_process_frequency_ = 0;
  Timestamp last_statistics_timestamp_ = 0;
  size_t consecutive_zupt_cnt_ = 0;

  size_t slam_pts_num_ = 0;
  size_t msckf_pts_num_ = 0;
  TrackingInfo tracking_info_;

  double cam_pose_delay_sum_ = 0;
  uint32_t cam_delay_cnt_ = 0;
  std::atomic<float> cam_pose_delay_avg_{0};

  int64_t last_process_boot_timestamp_ = 0;
  uint64_t debug_start_time_ns_ = 0;
};

}  // namespace vio
}  // namespace dm

#endif  // VIO_BACKEND_KEY_FRAME_STATE_ESTIMATOR_H_
