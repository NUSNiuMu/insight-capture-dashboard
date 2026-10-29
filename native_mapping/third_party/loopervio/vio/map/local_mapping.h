// Copyright 2020 DeepMirror Inc. All rights reserved.
#ifndef VIO_MAP_LOCAL_MAPPING_H_
#define VIO_MAP_LOCAL_MAPPING_H_

#include <atomic>
#include <deque>
#include <list>
#include <memory>
#include <set>
#include <string>
#include <unordered_set>
#include <vector>
#include <map>
#include "ceres/ceres.h"
#include "vio/thread/vio_thread.h"
#include "vio/map/map.h"
#include "vio/map/orb_matcher.h"
#include "vio/map/pose_sever.h"
#include "vio/map/ba_optimizer.h"
#ifndef DISABLE_RELOCALIZATION
#include "vio/map/relocalizer_with_map.h"
#endif

namespace dm::vio::lm {

class LoopClosure;

class LocalMapping : public VioThread {
 public:
  using Ptr = std::shared_ptr<LocalMapping>;

  LocalMapping();
  ~LocalMapping() override { ResetLocalMapping(); delete map_; }

  explicit LocalMapping(std::shared_ptr<DBoW3::Vocabulary> voc);

  void Run() override;

  void Start() override {
    LOG(INFO) << "LocalMapping thread started.";
    thread_ = std::thread(&LocalMapping::Run, this);
  }

  void Flush() override {}

  bool AddFrame(const FrameMeta::Ptr& frame) {
    std::lock_guard<std::mutex> lk(frame_buffer_mutex_);
    frame_buffer_.push_back(frame);
    localmapping_buffer_size_ = frame_buffer_.size();
    VLOG(1) << "AddFrame localmapping_buffer_size_ " << localmapping_buffer_size_;
    return true;
  }

  void PushIntrinsic(const std::vector<dm::common::ImageUndistortion>& intrinsic) {
    std::unique_lock<std::mutex> lk(intrinsic_buffer_mutex_);
    candidate_intrinsic_buffer_.push_back(intrinsic);
  }

  void SyncCameraIntrinsics() {
    std::unique_lock<std::mutex> lk(intrinsic_buffer_mutex_);
    if (candidate_intrinsic_buffer_.empty()) return;
    lm::KeyFrame::UpdateCameraIntrinsics(candidate_intrinsic_buffer_.back());
    std::vector<std::vector<dm::common::ImageUndistortion>>().swap(candidate_intrinsic_buffer_);
  }

  Map* map() const { return map_; }

  MapPointSet local_map_points() { return local_map_points_; }

  void Runt(const FrameMeta::Ptr& frame);

  // just for debug
  bool idle() { return localmapping_buffer_size_ == 0; }

  void SaveKeyFrameTraj();
  void ResetLocalMapping();

  LoopClosure* loop_closure();

#ifndef DISABLE_RELOCALIZATION
  void initialize_relocalizer(const RelocalizerWithMapConfig& config);
  RelocalizerWithMap* relocalizer() const { return relocalizer_.get(); }
#endif

  void set_loop_closure(LoopClosure* loop_closure);

  void DebugMessage(std::string tag);

  PoseServer::Ptr pose_server_ = nullptr;  // for debug
  std::atomic<int> localmapping_buffer_size_{0};
  Eigen::Vector6d latest_drift() {
    std::lock_guard<std::mutex> lk(latest_drift_mutex_);
    return latest_drift_;
  }

  bool TryAddNewFrame(FrameMeta::Ptr frm) {
    bool need_add_kf = false;
    TIMER_DECORATOR(need_add_kf = IsNewKeyFrame(frm), IsNewKeyFrame, 2);
    if (need_add_kf) {
      frm->odo_pose = frm->Pose();
      AddFrame(frm);
      last_added_frm_ = frm;
    }
    return need_add_kf;
  }

 private:
  bool IsFrameBufferEmpty() { return localmapping_buffer_size_ == 0; }

  FrameMeta::Ptr PopFrame() {
    std::lock_guard<std::mutex> lk(frame_buffer_mutex_);
    if (frame_buffer_.empty()) return nullptr;
    auto frm = frame_buffer_.front();
    frame_buffer_.pop_front();
    localmapping_buffer_size_ = frame_buffer_.size();
    return frm;
  }

  bool IsNewKeyFrame(const FrameMeta::Ptr& candidate_frame);

  void AddKeyFrameToMap();

  // search the map points in current master keyframe
  size_t SearchPointsInLastKeyFrame();
  void UpdateLocalKeyFrames();
  size_t UpdateLocalPoints();
  void SearchLocalPoints();
  void GetMaxMinDistanceInvariance(MapPoint* pt, float* max_dist, float* min_dist);

  size_t CreateNewMapPointByStereo();

  size_t CreateNewMapPointByNeibour();

  size_t CreateNewMapPoint();

  bool IsBusy();

  size_t FuseWithNeighbors();

  void MapPointCulling();

  size_t KeyFrameCulling();

  void MapPointFiltering(const std::unordered_set<MapPoint*>& mpts,
                         std::unordered_set<KeyFrame*> filtered_fixed_kfs);

  void LocalBA();

  bool GrabLocalMapInLBA(
      KeyFrameSet* kfs_need_optimize, MapPointOptimizeSet* mappoints_need_optimize,
      KeyFrameSet* filtered_fixed_kfs,
      std::map<FrameId, std::map<FeatureId, Eigen::Vector3d>>* isolated_stereo_points,
      std::unordered_set<KeyFrame*>* kfs_actual_in_optimize,
      std::unordered_set<KeyFrame*>* fixed_kfs_actual_in_optimize,
      std::unordered_set<MapPoint*>* mpts_actual_in_optimize);
  int AddPtToProblem(KeyFrameSet* kfs_need_optimize, MapPointOptimizeSet* mappoints_need_optimize,
                     KeyFrameSet* filtered_fixed_kfs,
                     std::unordered_set<KeyFrame*>* kfs_actual_in_optimize,
                     std::unordered_set<KeyFrame*>* fixed_kfs_actual_in_optimize,
                     std::unordered_set<MapPoint*>* mpts_actual_in_optimize,
                     BAOptimizer* optimizer);
  void AddRelativePoseToProblem(KeyFrameSet* filtered_fixed_kfs,
                                std::unordered_set<KeyFrame*>* kfs_actual_in_optimize,
                                std::unordered_set<KeyFrame*>* fixed_kfs_actual_in_optimize,
                                int coeff, BAOptimizer* optimizer);
  void ConstructBA(KeyFrameSet* kfs_need_optimize, MapPointOptimizeSet* mappoints_need_optimize,
                   KeyFrameSet* filtered_fixed_kfs,
                   std::unordered_set<KeyFrame*>* kfs_actual_in_optimize,
                   std::unordered_set<KeyFrame*>* fixed_kfs_actual_in_optimize,
                   std::unordered_set<MapPoint*>* mpts_actual_in_optimize);
  void UpdateIsolatedPt(
      std::map<FrameId, std::map<FeatureId, Eigen::Vector3d>>* isolated_stereo_points,
      std::unordered_set<KeyFrame*>* kfs_actual_in_optimize);

  size_t PnpOptimize();

  void UpdateParentOfCurrentKeyFrame();

  double ComputeProjectError(MapPoint* pt3d, double* time_span);


 private:
  Map* map_ = nullptr;

  std::mutex frame_buffer_mutex_;
  std::deque<FrameMeta::Ptr> frame_buffer_;

  KeyFrame* current_key_frame_ = nullptr;

  // current local map
  KeyFrameSet local_key_frames_;
  MapPointSet local_map_points_;

  std::unique_ptr<ORBextractor> orb_extractor_ = nullptr;

  std::set<FeatureId> recent_add_map_points_;
  std::vector<MapPoint*> current_kf_create_map_points_;

  ORBMatcher orb_matcher_;

  LoopClosure* loop_closure_ = nullptr;

#ifndef DISABLE_RELOCALIZATION
  std::unique_ptr<RelocalizerWithMap> relocalizer_ = nullptr;
#endif

  std::mutex latest_drift_mutex_;
  Eigen::Vector6d latest_drift_ = Eigen::Vector6d::Zero();

  FrameMeta::Ptr last_added_frm_ = nullptr;
  std::mutex intrinsic_buffer_mutex_;
  std::vector<std::vector<dm::common::ImageUndistortion>> candidate_intrinsic_buffer_;
};

cv::Mat DrawMapPointsInKeyFrame(KeyFrame* kf, CameraId camera_id);

cv::Mat GetPatch(cv::Mat input, cv::Point center, int patch_width, int patch_height, int* start_x,
                 int* start_y);

cv::Mat DrawProjectionErrors(MapPoint* mpt, size_t patch_size, size_t rows, size_t cols);

class EndOptimizationCallback : public ceres::IterationCallback {
 public:
  explicit EndOptimizationCallback(LocalMapping* ptr) : local_mapping_(ptr) {}
  ceres::CallbackReturnType operator()(const ceres::IterationSummary& summary) {
    if (local_mapping_ && local_mapping_->localmapping_buffer_size_ > 1) {
      VLOG(2) << "end optimization cause of localmapping_buffer_size_ "
                << local_mapping_->localmapping_buffer_size_;
      return ceres::SOLVER_TERMINATE_SUCCESSFULLY;
    }
    return ceres::SOLVER_CONTINUE;
  }

 private:
  LocalMapping* local_mapping_ = nullptr;
};

}  // namespace dm::vio::lm

#endif  // VIO_MAP_LOCAL_MAPPING_H_
