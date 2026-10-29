// Copyright 2020 DeepMirror Inc. All rights reserved.
#ifndef VIO_MAP_LOOP_CLOSURE_H_
#define VIO_MAP_LOOP_CLOSURE_H_

#include <deque>
#include <map>
#include <memory>
#include <string>
#include <vector>
#include "opencv2/opencv.hpp"
#include "vio/map/ba_optimizer.h"
#include "vio/map/map.h"
#include "vio/map/orb_matcher.h"
#include "vio/map/pose_sever.h"
#include "vio/thread/vio_thread.h"

namespace dm::vio::lm {

class LocalMapping;

class LoopClosure : public vio::VioThread {
 public:
  using Ptr = std::shared_ptr<LoopClosure>;
  struct Evidence {
    size_t matches=0, inliers=0, cells=0;
    double rms_px=0;
    KeyFrame* reference=nullptr;
    std::vector<std::pair<size_t, FeatureId>> observations;
    size_t verified_references=0;
    double condition_ratio=0;
    const char* reason="insufficient_retained_landmarks";
  };
  Evidence evidence() const { return evidence_; }
  LoopClosure() {}

  LoopClosure(Map* map, LocalMapping* local_mapping);

  void Flush() {}

  void Run();
  bool Process(KeyFrame* current);
  std::optional<dm::math::Rigid3d> Localize(KeyFrame* query);

  std::optional<dm::math::Rigid3d> OptimizeLoopPose(KeyFrame* query_kf,
                                                    KeyFrame* loop_kf,
                                                    const math::Rigid3d& query_Twi);
  std::optional<dm::math::Rigid3d> RefinePose(KeyFrame* query_kf,
      const std::vector<MapPoint*>& matched, const math::Rigid3d& initial,
      size_t minimum_inliers, double minimum_ratio);

  void AddLoopBuffer(FrameId frm_id) {
    std::lock_guard<std::mutex> lg(loop_buffer_mutex_);
    loop_buffer_.push_back(frm_id);
  }

  PoseServer::Ptr pose_server_ = nullptr;  // for debug

  void UpdateKeyFramePoses();

  void RunPoseGraph();

  std::string GetLoopInfo();

 private:
  KeyFrame* PopLoopBuffer() {
    std::lock_guard<std::mutex> lg(loop_buffer_mutex_);
    if (loop_buffer_.empty()) return nullptr;

    while (loop_buffer_.size() > 3) {
      loop_buffer_.pop_front();
    }

    auto frm_id = loop_buffer_.front();
    loop_buffer_.pop_front();
    return map_->GetKeyFrameTS(frm_id);
  }

  std::optional<dm::math::Rigid3d> SolveLoopPose(KeyFrame* query_kf,
                                                 KeyFrame* loop_kf,
                                                 int min_inlier = 5);


  float ComputeBowMatchThreshold(KeyFrame*) { return .03f; }

  cv::Mat DrawMatches(KeyFrame* query_kf,
                      KeyFrame* loop_kf,
                      const std::vector<cv::DMatch>& matches);

  cv::Mat DrawMapPoints(KeyFrame* kf);

  std::optional<dm::math::Rigid3d> SolveLoopPose2(KeyFrame* query_kf, KeyFrame* loop_kf);

  Evidence evidence_;
  Map* map_;
  std::mutex loop_buffer_mutex_;
  std::deque<FrameId> loop_buffer_;

  LocalMapping* local_mapping_ = nullptr;

  BAOptimizerTS optimizer_;

  std::mutex loop_info_mutex_;
  std::string loop_info_;
};
}  // namespace dm::vio::lm

#endif  // VIO_MAP_LOOP_CLOSURE_H_
