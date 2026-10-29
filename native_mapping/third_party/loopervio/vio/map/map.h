// Copyright 2020 DeepMirror Inc. All rights reserved.
#ifndef VIO_MAP_MAP_H_
#define VIO_MAP_MAP_H_

#include <map>
#include <memory>
#include <string>
#include <unordered_map>
#include <vector>
#include <set>
#include "vio/map/key_frame.h"
#include "vio/map/map_point.h"

namespace dm::vio::lm {

class KeyFrame;
class MapPoint;

struct SimilarKfInfo {
  SimilarKfInfo() {}
  SimilarKfInfo(KeyFrame* _kf, double _score) : kf(_kf), score(_score) {}
  KeyFrame* kf;
  double score;
};

class Map {
 public:
  Map() {}
  explicit Map(std::shared_ptr<DBoW3::Vocabulary> voc) {

  }

  int KeyFramesSize();
  bool KeyFrameEmpty();
  int MapPointsSize();
  bool MapPointEmpty();
  KeyFrame* GetKeyFrame(FrameId id) const {
    if (!key_frames_.count(id)) return nullptr;
    return key_frames_.at(id);
  }

  KeyFrame* GetKeyFrameTS(FrameId id) {
    std::lock_guard<std::mutex> lg(kf_mutex_);
    if (!key_frames_.count(id)) return nullptr;
    auto kf = key_frames_[id];
    kf->forced_retention_ = true;
    return key_frames_[id];
  }

  KeyFrame* LastKeyFrame() {
    if (key_frames_.empty()) return nullptr;
    return key_frames_.rbegin()->second;
  }

  void AddKeyFrame(KeyFrame* kf);

  void AddMapPoint(MapPoint* map_point);

  void EraseMapPoint(MapPoint* map_point);

  bool EraseMapPointTS(MapPoint* map_point);

  void EraseMapPoint(FeatureId mpt_id);

  MapPoint* GetMapPoint(FeatureId mpt_id) {
    if (map_points_.count(mpt_id)) return map_points_[mpt_id];
    return nullptr;
  }

  bool EraseKeyFrameTS(KeyFrame *kf);

  void EraseKeyFrame(KeyFrame *kf);

  std::vector<KeyFrame *> GetBestCovisibilityKeyFramesTS(KeyFrame* query_kf, int n = -1);
  std::map<FrameId, KeyFrame*> key_frames() const { return key_frames_; }

  std::map<FeatureId, MapPoint*> map_points() const { return map_points_; }
  int MapPointsInMap() { return map_points_.size(); }
  void ObservationCountAnalyze(const std::string& tag = "");

  void set_enable_erase_flag(bool flag) {
    std::lock_guard<std::mutex> lg(enable_erase_mutex_);
    enable_erase_flag_ = flag;
  }

  void RealEraseData() {
    std::lock_guard<std::mutex> lg(enable_erase_mutex_);
    if (!enable_erase_flag_) return;
    // todo: erase mark data
  }

  void SaveTraj(const std::string& path);

  std::vector<SimilarKfInfo> FindSimilarFrames(KeyFrame* query_kf, size_t max_size,
                                               int min_id_dist);

  std::vector<SimilarKfInfo> RetrieveSimilarFrames(const DBoW3::BowVector& bow_vec,
                                                   size_t max_size) const;

  void UpdateKeyFramePoses(const std::map<FrameId, Eigen::Vector6d>& new_kf_poses);

  void CheckMap(const std::string& tag = "");

  void Reset();

  // High-risk operations, must be used in pairs
  void AcquireMptMutex() {
    CHECK_EQ(mpt_mutex_ptr_, nullptr);
    std::unique_lock<std::mutex> lock(mpt_mutex_);
    mpt_mutex_ptr_ = lock.release();
  }

  void ReleaseMptMutex() {
    CHECK_NE(mpt_mutex_ptr_, nullptr);
    mpt_mutex_ptr_->unlock();
    mpt_mutex_ptr_ = nullptr;
  }

  void TryClearKeyFrames();

  void TryClearMapPoints();

 protected:
  // <id, LocalMappingKeyFrame>
  std::mutex kf_mutex_;
  std::mutex mpt_mutex_;
  std::mutex* mpt_mutex_ptr_ = nullptr;
  std::map<FrameId, KeyFrame*> key_frames_;
  std::map<FeatureId, MapPoint*> map_points_;

  //
  std::mutex enable_erase_mutex_;
  std::atomic_bool enable_erase_flag_{true};

  mutable std::mutex db_mutex_;
  std::shared_ptr<DBoW3::Database> db_;

  std::unordered_map<DBoW3::EntryId, FrameId> entry_id_to_kfs_;
  std::map<FrameId, DBoW3::EntryId> kf_to_entry_ids_;

  std::set<KeyFrame*> delete_key_frames_buffer_;
  std::set<MapPoint*> delete_map_points_buffer_;

  // only used to debug
  std::unordered_map<MapPoint*, uint32_t> pt_obs_cnt_;
  std::map<KeyFrame*, uint32_t, KeyFrameCmp> kf_obs_cnt_;
};

}  // namespace dm::vio::lm

#endif  // VIO_MAP_MAP_H_
