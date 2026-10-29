// Copyright 2020 DeepMirror Inc. All rights reserved.
#ifndef VIO_MAP_MAP_POINT_H_
#define VIO_MAP_MAP_POINT_H_

#include <map>
#include <set>
#include <string>
#include <unordered_map>
#include "vio/map/key_frame.h"

namespace dm::vio::lm {
class KeyFrame;

class MapPoint;

struct MapPointCmp {
  bool operator()(const MapPoint* mpt0, const MapPoint* mpt1) const;
};

struct MapPointOptimizeCmp {
  bool operator()(const MapPoint* mpt0, const MapPoint* mpt1) const;
};

using MapPointSet = std::set<MapPoint*, MapPointCmp>;
using MapPointOptimizeSet = std::set<MapPoint*, MapPointOptimizeCmp>;

class MapPoint {
 public:
  struct Observation {
    Observation() {}
    explicit Observation(CameraId _cam_id) : cam_id(_cam_id) {}
    Observation(CameraId _cam_id, size_t _feat_id, CameraRole _cam_role)
        : cam_id(_cam_id), feat_id(_feat_id), cam_role(_cam_role) {}

    CameraId cam_id;
    size_t feat_id;
    CameraRole cam_role;
    bool operator<(const Observation& other) const { return cam_id < other.cam_id; }
    struct Hash {
      size_t operator()(const Observation& a) const { return std::hash<int>()(a.cam_id); }
    };
    bool operator==(const Observation& other) const { return cam_id == other.cam_id; }
  };

  struct FrustumInfo {
    // Variables used by the tracking this map point in a keyframe
    Eigen::Vector2d project_uv;
    double depth;
    double view_cos;
    bool in_view = false;
    int scale_level = -1;
    float distance = -1.0;
  };

  struct PointState {
    bool fixed;
    bool optimized_at_last_time;
    int optimize_num;
    bool refined = false;
  };

  using KeyFrame2Observations = std::map<KeyFrame*, std::set<Observation>, KeyFrameCmp>;

  FeatureId point_id() const { return point_id_; }

  MapPoint() { point_id_ = public_self_inc_id_++; }
  MapPoint(Eigen::Vector3d pt, KeyFrame* kf);
  ~MapPoint() {}

  void IncreaseVisible(int n = 1) { num_visible_ += n; }
  void IncreaseFound(int n = 1) { num_found_ += n; }
  void DecreaseFound(int n = 1) { num_found_ -= n; }

  float FoundRatio();
  cv::Mat GetDescriptor() { return descriptor_.clone(); }
  bool IsInKeyFrame(KeyFrame* kf);
  void AddObservation(KeyFrame* kf, CameraId cam_id, size_t feat_idx, const std::string& tag = "");
  void AddObservation(KeyFrame* kf, std::set<Observation> obs);  // not used
  void EraseObservation(KeyFrame* kf, CameraId cam_id);
  void EraseObservation(KeyFrame* kf);

  std::optional<Observation> observations(KeyFrame* kf, CameraId cam_id) {
    if (!observations_.count(kf)) return std::nullopt;
    auto it = observations_[kf].find(Observation(cam_id));
    if (it == observations_[kf].end()) return std::nullopt;
    return *it;
  }

  std::optional<std::set<Observation>> observations(KeyFrame* kf) {
    if (!observations_.count(kf)) return std::nullopt;
    return observations_[kf];
  }

  bool IsInFrustum(KeyFrame* kf, float viewing_cos_limit, CameraId cam_id,
                   FrustumInfo* frustum_info = nullptr);

  KeyFrame2Observations& observations() { return observations_; }

  bool IsBad() { return is_bad_; }
  void SetBad() { is_bad_ = true; }

  void ReplacedByMapPoint(MapPoint* map_point, const std::string& tag = "");

  uint32_t NumOfObs();

  double* point3d_data() { return point3d_.data(); }
  const double* point3d_data() const { return point3d_.data(); }
  Eigen::Vector3d GetNormal() { return normal_vector_; }

  void ComputeDescriptorsAndNormal(bool incremental_calculate = false);

  const Eigen::Vector3d& WorldPoint3d() const { return point3d_; }

  void SetWorldPoint3d(const Eigen::Vector3d& world_pt) { point3d_ = world_pt; }

  uint32_t NumOfAllObs() const;
  bool CheckAllObsValidateIn(MapPoint* pt);

  bool OnlyOneObservation() { return observations_.size() == 1; }

  float ComputeTriAngle(float percent);

  void RefreshAhp(KeyFrame* new_anchor = nullptr, CameraId anchor_cam = INVALID_CAM_ID) {
    if (new_anchor) {
      anchor_kf_ = new_anchor;
      anchor_cam_ = anchor_cam;
    }
    dm::math::Rigid3d Twc = anchor_kf_->camera_pose(anchor_cam_);
    Eigen::Vector3d Pc = Twc.Inverse() * point3d_;
    double inverse_depth = 1.0 / Pc[2];
    Eigen::Vector2d xy(Pc[0] * inverse_depth, Pc[1] * inverse_depth);
    anchor_postion.head(2) = xy;
    anchor_postion[2] = inverse_depth;
  }

  void RefreshWorldPointByAhp() {
    Eigen::Vector3d pt(anchor_postion);
    pt[2] = 1;
    pt *= anchor_postion[2];
    dm::math::Rigid3d Twc = anchor_kf_->camera_pose(anchor_cam_);
    point3d_ = Twc * pt;
  }
  int PredictScale(float distance, float max_dist);
  PointState& state() { return state_; }
  const PointState& state() const { return state_; }

  void SetMinDistance(float distance) { min_distance_ = distance; }
  void SetMaxDistance(float distance) { max_distance_ = distance; }

  float min_distance() { return min_distance_; }
  float max_distance() { return max_distance_; }

  FrameId first_keyframe_seen() { return first_keyframe_seen_; }
  void UpateLastKFSeen(FrameId kf_id) { last_keyframe_seen_ = kf_id; }
  FrameId last_keyframe_seen() { return last_keyframe_seen_; }
  std::unordered_map<KeyFrame*, FrustumInfo>& frustum_infos() {
    return frustum_infos_;
  }
  FrameId ref_keyframe() { return ref_keyframe_; }

  void set_forced_retention(bool state) { forced_retention_ = state; }

  bool forced_retention() {return forced_retention_;}

  static FeatureId public_self_inc_id_;

 private:
  // Best descriptor to fast matching
  cv::Mat descriptor_;
  FrameId last_keyframe_seen_;
  FrameId first_keyframe_seen_;
  FrameId ref_keyframe_;

  PointState state_;

  KeyFrame2Observations observations_;

  // Pw=[x,y,z]
  Eigen::Vector3d point3d_;

  // Mean viewing direction
  Eigen::Vector3d normal_vector_;

  // Scale invariance distances
  float min_distance_;
  float max_distance_;

  // anchor coordinate [u,v,1/depth] relative to first_keyframe_seen_
  // TODO(yixuan): change the ref_kf when ref_kf has been deleted

  Eigen::Vector3d anchor_postion;
  KeyFrame* anchor_kf_;
  CameraId anchor_cam_;

  FeatureId point_id_;

  // tracking counter, Not affected by kf deletion
  int num_visible_ = 0;
  int num_found_ = 0;

  // useful when replacing map point
  bool is_bad_ = false;

  std::unordered_map<KeyFrame*, FrustumInfo> frustum_infos_;

  std::atomic_bool forced_retention_ = false;
};
}  // namespace dm::vio::lm
#endif  // VIO_MAP_MAP_POINT_H_
