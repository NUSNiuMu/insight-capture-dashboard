// Copyright 2020 DeepMirror Inc. All rights reserved.

#ifndef VIO_POSE_MAINTAINER_H_
#define VIO_POSE_MAINTAINER_H_

#include <deque>
#include <map>
#include <tuple>
#include <vector>
#include "vio/backend/sliding_window_params.h"
#include "vio/imu_model.h"
namespace dm {
namespace vio {
class PoseMaintainer {
 public:
  // Injected once at construction time; see vio/imu_model.h.
  void SetImuModel(const ImuModel& m) { imu_model_ = m; }

 private:
  ImuModel imu_model_;
  bool repropagate_ = false;
  // Deque of IMU measurements, measurement timestamps are in increasing order.
  std::deque<std::tuple<uint64_t, Eigen::Vector3d, Eigen::Vector3d>> imu_measurements_;
  std::mutex measurements_mutex_;

  std::deque<FramePose> frames_;
  std::mutex deque_frames_mutex_;

  FramePose latest_imu_pose_;
  std::mutex latest_imu_mutex_;

  FramePose latest_frame_pose_;
  std::mutex latest_frame_mutex_;
  size_t sliding_window_size_ = 11;

  std::deque<Timestamp> imu_pose_timestamps_;
  uint32_t imu_pose_frequency_ = 0;
  std::deque<Timestamp> cam_pose_timestamps_;
  uint32_t cam_pose_frequency_ = 0;
  double predict_period_based_on_imu_ = 0;
  // Serializes concurrent calls to GetIMUPoseEstimate. The is_custom=false path (VioGetLatestPose)
  // and the is_custom=true path (NaiveVio::ProcessImages, frontend thread) run concurrently and
  // both modify imu_pose_timestamps_ / predict_period_based_on_imu_ without consistent locking.
  std::mutex get_imu_pose_estimate_mutex_;

 public:
  PoseMaintainer() {}
  explicit PoseMaintainer(size_t sliding_window_size) : sliding_window_size_(sliding_window_size) {}
  ~PoseMaintainer() {}

  void AddIMU(const uint64_t timestamp, const Eigen::Vector3d& linear_acceleration,
              const Eigen::Vector3d& angular_velocity);
  void UpdateStatus(uint64_t timestamp, FrameState state, std::deque<FramePose> frames);
  // If is_custom is true, we will also return the predicted period (unit: s) compared
  // with the latest timestamp of the camera pose.
  // linear_velocity and linear_acceleration are in world coordinate.
  void GetIMUPoseEstimate(uint64_t* timestamp, FramePose* state, bool is_custom,
                          double* predict_period, Eigen::Vector3d* linear_velocity = nullptr,
                          Eigen::Vector3d* angular_velocity = nullptr,
                          Eigen::Vector3d* linear_acceleration = nullptr);

  FrameState Interpolation(FrameState frame_start, FrameState frame_end, uint64_t timestamp);
  std::tuple<std::vector<Eigen::Vector3d>, std::vector<Eigen::Vector3d>, std::vector<double>>
  GetImuMeasurement(const uint64_t& start_time, uint64_t* end_time);
  void set_sliding_window_size(size_t sw_size) { sliding_window_size_ = sw_size; }
  /**
   * @brief locate the specify timestamp in frame0, frame1,...,frame10, latest_result_timestamp,
   * latest_result_timestamp>=frame10, because of zupt
   *
   * @param timestamp the specify timestamp
   * @param latest_frame_state the latest frame state, namely latest_frame_state_
   * @param start
   * @param end
   * @return int
   * 1: frame10->timestamp <= timestamp <= latest_frame_timestamp, end is correspond state. when
   * timestamp == frame10->timestamp, end = frame10, otherwise end = latest_frame_state. 2: the
   * specify timestamp > the latest frame timestamp, end is the latest frame state. 3: the specify
   * timestamp is in [frame0, ..., frame10], start->timestamp <= timestamp < end->timestamp. 4:
   * timestamp < frame0->timestamp, start and end is useless in this case
   */
  int LocalizationTimestamp(uint64_t timestamp, FramePose latest_frame_state, FramePose* start,
                            FramePose* end);
  FramePose Interpolation(FramePose frame_start, FramePose frame_end, uint64_t timestamp);

  int GetFramePoseByTimestamp(uint64_t timestamp, math::Rigid3d* Twi);

  uint32_t GetPoseFrequencyImu() { return imu_pose_frequency_; }
  uint32_t GetPoseFrequencyCam() { return cam_pose_frequency_; }
  double GetPredictPeriodBasedOnImu() { return predict_period_based_on_imu_; }
};
}  // namespace vio
}  // namespace dm

#endif  //  VIO_POSE_MAINTAINER_H_
