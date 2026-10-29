// Copyright 2020 DeepMirror Inc. All rights reserved.

#ifndef VIO_NAIVE_VIO_H_
#define VIO_NAIVE_VIO_H_

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <deque>
#include <functional>
#include <map>
#include <memory>
#include <string>
#include <thread>
#include <unordered_map>
#include <utility>
#include <vector>
#include <opencv2/opencv.hpp>
#include "config/vio_config.h"
#include "vio/anchor_point/anchor_point.h"
#include "vio/imu_model.h"
#include "vio/data_io/vio_bin_io.h"
#include "vio/imu_interpolation.h"
#include "vio/backend/key_frame_state_estimator.h"
#include "vio/vio.h"

#ifdef PSEUDO_FRONT_END
#include "vio/frontend/pseudo_front_end.h"
#else
#include "vio/frontend/fast_optical_flow_front_end.h"
#endif

#ifdef __x86_64__
#define FRONTEND_DEBUG_MODE 1
#else
#define FRONTEND_DEBUG_MODE 0
#endif

namespace dm {
namespace vio {
// Naive in the sense that it ignores the backend latency.
class NaiveVio : public Vio {
 public:
  NaiveVio() = delete;
  NaiveVio(const VioConfig& config,
           const std::vector<dm::common::ImageUndistortion>& image_undistortions,
           const dm::math::Rigid3d& pose_camera_in_imu, const ImuModel& imu_model);

  // `imu_model` carries the calibrated IMU intrinsics and is deliberately NOT defaulted: the
  // model has to be identical everywhere it is applied, and a defaulted argument is exactly
  // how one construction site ends up silently running the uncalibrated model. Pass
  // ImuModel() explicitly when there is no calibration.
  NaiveVio(const VioConfig& config,
           const std::vector<dm::common::ImageUndistortion>& image_undistortions,
           const std::vector<dm::math::Rigid3d>& poses_camera_in_imu, const ImuModel& imu_model);

  void AddImage(const uint64_t timestamp, const cv_mini::Mat& image) override;
  void AddImage(const uint32_t camera_id, const uint64_t timestamp,
                const cv_mini::Mat& image) override;

  // 注册回调：在每帧 frontend（BuildPyramid + KLT）处理完成后被调用
  // 从 vio_frontend 线程触发，回调应轻量（仅做通知）
  using FrontendDoneCallback = std::function<void()>;
  void SetFrontendDoneCallback(FrontendDoneCallback cb) { frontend_done_cb_ = std::move(cb); }

  // 注册回调：在每帧 frontend 开始处理前（ProcessImages 之前）被调用
  // 与 SetFrontendDoneCallback 配对使用，精确标定 frontend 占用 DDR 的时间窗口
  using FrontendStartCallback = std::function<void()>;
  void SetFrontendStartCallback(FrontendStartCallback cb) { frontend_start_cb_ = std::move(cb); }

  void AddVioFrame(const uint64_t timestamp, const dm::vio::VioProblemFrame& frame);

  void AddMultiVioFrame(const uint64_t timestamp, const dm::vio::MultiVioProblemFrame& frame);

  void AddImuMeasurement(const uint64_t timestamp, const Eigen::Vector3d& linear_acceleration,
                         const Eigen::Vector3d& angular_velocity) override;

  void AddAcceleration(const uint64_t timestamp, const Eigen::Vector3d& linear_acceleration);

  void AddAngularVelocity(const uint64_t timestamp, const Eigen::Vector3d& angular_velocity);

  // If this state estimator has not initialized yet, we return a timestamp of 0 to indicate that.
  void GetLatestStateEstimate(uint64_t* timestamp, dm::vio::FrameState* state) const override;

  void GetLatestKeyFrameStateEstimate(uint64_t* timestamp, dm::vio::FrameState* state);

  // Return the confidence of the pose, which is calculated based on the length
  // of the prediction time.
  double GetImuPoseByTimestamp(uint64_t* timestamp, dm::math::Rigid3d* pose, bool is_custom = false,
                               Eigen::Vector3d* linear_velocity = nullptr,
                               Eigen::Vector3d* angular_velocity = nullptr,
                               Eigen::Vector3d* linear_acceleration = nullptr);

  int GetFramePoseByTimestamp(uint64_t timestamp, math::Rigid3d* Twi);

  // Returns the 6x6 pose covariance (rotation 3 + translation 3) of the latest image frame.
  // raw_timestamp is the original camera frame timestamp; it is 0 until the first Filtering().
  void GetLatestFramePoseCovariance(uint64_t* raw_timestamp,
                                    Eigen::Matrix<double, 6, 6>* cov) const {
    key_frame_state_estimator_->GetLatestFramePoseCovariance(raw_timestamp, cov);
  }

  uint64_t GetLatestProcessedCameraTimestamp() const {
    return key_frame_state_estimator_->latest_processed_raw_timestamp();
  }

  uint64_t GetLatestSubmittedCameraTimestamp() const {
    return latest_backend_submitted_timestamp_.load(std::memory_order_acquire);
  }

  uint64_t GetLatestFinalizedCameraTimestamp() const {
    return latest_frontend_finalized_timestamp_.load(std::memory_order_acquire);
  }

  Eigen::Matrix3d GetInitR();

  dm::math::Rigid3d GetCameraExtrinsic(CameraId cam_id);

  dm::common::ImageUndistortion GetCameraIntrinsic(CameraId camera_id);

  void GetSystemStatus(VioStatusInfo* status_info);

  std::vector<Point3d> GetVioPoints3d();

  bool IsInitialized() { return key_frame_state_estimator_->system_initialized(); }

  void StopBackend();

  void SaveMapPose(const std::string& path);

  std::string GetLoopInfo();

  uint32_t GetPoseFrequencyImu();
  uint32_t GetPoseFrequencyCam();
  double GetPredictPeriodBasedOnImu();

  void SetVioBinWriter(const VioBinWriter::Ptr& writer) { bin_writer_ = writer; }

  std::optional<AnchorPoint::AnchorResult> GetAnchorPointInWorld(float x, float y,
                                                                 Timestamp timestamp) {
    if (!anchor_point_) return std::nullopt;
    return anchor_point_->GetPoint3d(x, y, timestamp);
  }

  std::optional<AnchorPoint::AnchorResult> GetAnchorPointInWorldByIdx(float x, float y,
                                                                      uint32_t idx) {
    if (!anchor_point_) return std::nullopt;
    return anchor_point_->GetPoint3dByIdx(x, y, idx);
  }

  std::vector<Point3d> vio_point3ds() { return key_frame_state_estimator_->vio_point3ds(); }

  lm::Map* map() const { return key_frame_state_estimator_->map(); }
  lm::LocalMapping::Ptr local_mapping() const {
    return key_frame_state_estimator_->local_mapping();
  }
  void GetDebugInfo(std::string* info);

  std::vector<float> GetCamPoseDelay();

  void set_enable_viewer(bool enable) { enable_viewer_ = enable; }

  // Enable debug matching visualization + IMU state logging after t seconds from session start.
  // t < 0 to disable. Visualization blocks until keypress ('q' to stop).
  void SetDebugStartTimeSec(double t);

#ifndef __EMSCRIPTEN__
  dm::recon::Mesh GetSceneMesh() { return key_frame_state_estimator_->GetPlaneMesh(); }
#endif

 private:
  std::optional<dm::vio::VioProblemFrame> ProcessImage(Timestamp cur_time, CameraId cam_id,
                                                       const cv::Mat& image,
                                                       uint64_t imu_pose_timestamp,
                                                       const dm::math::Rigid3d& cur_imu_pose,
                                                       double imu_pose_confidence);
  void ProcessImages(dm::vio::MultiVioProblemFrame* multi_vio_frame);

  cv::Mat DrawOpticalFlow();

  cv::Mat DrawLandMarks(const FrameMeta::Ptr frame, const std::vector<LandmarkMeta::Ptr>& landmarks,
                        CameraId cam_id, const cv::Mat& image);
#ifdef PSEUDO_FRONT_END
  PseudoFrontEnd frontend_;
#else
  FastOpticalFlowFrontEnd frontend_;
#endif
  KeyFrameStateEstimator::Ptr key_frame_state_estimator_ = nullptr;

  std::atomic<uint64_t> first_imu_time_{0};

  std::chrono::time_point<std::chrono::system_clock> launch_time_;

  size_t num_cams_ = 0;
  std::map<CameraId, cv::Mat> cur_images_;
  Timestamp cur_timestamp_ = 0;

  std::map<Timestamp, std::map<CameraId, cv::Mat>> images_;
  std::unordered_map<CameraId, dm::math::Rigid3d> last_frontend_imu_pose_;
  std::unordered_map<CameraId, Timestamp> last_frontend_imu_pose_timestamp_;
  uint64_t max_rotation_prior_interval_ns_ = 0;

  ImuInterpolation imu_interpolation_;

  std::map<CameraId, std::unordered_map<uint32_t, std::vector<cv::Point2f>>> latest_features_;
  std::unordered_map<CameraId, cv::Mat> last_draw_image_;
  uint32_t slave_skip_interval_ = 1;

  VioBinWriter::Ptr bin_writer_ = nullptr;

  AnchorPoint::Ptr anchor_point_;
  dm::common::Timer::Ptr tFE_;
  FrontendDoneCallback frontend_done_cb_;
  FrontendStartCallback frontend_start_cb_;
  std::atomic<uint64_t> latest_backend_submitted_timestamp_{0};
  std::atomic<uint64_t> latest_frontend_finalized_timestamp_{0};

  std::string debug_info_;

  double debug_start_time_s_ = -1.0;
  bool debug_estimator_time_set_ = false;

  double cam_preparation_delay_sum_ = 0;
  uint32_t cam_delay_cnt_ = 0;
  std::atomic<float> cam_preparation_delay_avg_{0};

  // Library/C-API users are headless by default. Debug tools that want the
  // OpenCV tracking windows opt in through set_enable_viewer(true).
  bool enable_viewer_ = false;
};

}  // namespace vio
}  // namespace dm

#endif  // VIO_NAIVE_VIO_H_
