// Copyright 2020 DeepMirror Inc. All rights reserved.
#ifndef VIO_MAP_KEY_FRAME_H_
#define VIO_MAP_KEY_FRAME_H_

#include <atomic>
#include <functional>
#include <map>
#include <memory>
#include <set>
#include <string>
#include <vector>
#include "vio/data_types.h"
#include "vio/frontend/orb_extractor.h"
#ifdef CMAKE_BUILD
#include "DBoW3/DBoW3.h"
#else
#include "DBoW3.h"
#endif
#include "vio/map/common.h"

namespace dm::vio::lm {

class MapPoint;

class KeyFrame;

struct KeyFrameCmp {
  bool operator()(const KeyFrame* kf1, const KeyFrame* kf2) const;
};

using KeyFrameSet = std::set<KeyFrame*, KeyFrameCmp>;

struct LoopInfo {
  LoopInfo() {}
  explicit LoopInfo(FrameId _loop_frm) : loop_frm(_loop_frm) {}
  LoopInfo(FrameId _query_frm, FrameId _loop_frm, const dm::math::Rigid3d &_Tlq)
      : query_frm(_query_frm), loop_frm(_loop_frm), Tlq(_Tlq) {}
  FrameId query_frm;
  FrameId loop_frm;
  dm::math::Rigid3d Tlq;  // query frame pose in loop frame body coord
  bool operator<(const LoopInfo &other) const {
    return loop_frm < other.loop_frm;
  }
};

template <typename T>
class ImgGrid {
 public:
  ImgGrid() {}
  ImgGrid(size_t width, size_t height, size_t grid_width, size_t grid_height)
      : width_(width), height_(height), grid_width_(grid_width), grid_height_(grid_height) {
    rows_ = std::ceil(height_ / grid_height_);
    cols_ = std::ceil(width_ / grid_width_);
    data_.resize(rows_ * cols_);
  }

  T& at(size_t r, size_t c) { return data_[r * cols_ + c]; }

  const T& at(size_t r, size_t c) const { return data_[r * cols_ + c]; }

  T& operator()(float x, float y) {
    size_t r = RowIndex(y);
    size_t c = ColIndex(x);
    return data_[r * cols_ + c];
  }

  const T& operator()(float x, float y) const {
    size_t r = RowIndex(y);
    size_t c = ColIndex(x);
    return data_[r * cols_ + c];
  }
  size_t RowIndex(float value) const {
    if (value < 0) return 0;
    size_t ret = floor(value / grid_height_);
    return ret > rows_ - 1 ? rows_ - 1 : ret;
  }
  size_t ColIndex(float value) const {
    if (value < 0) return 0;
    size_t ret = floor(value / grid_width_);
    return ret > cols_ - 1 ? cols_ - 1 : ret;
  }

  size_t SafeRowIndex(float value) const {
    if (value < 0) return 0;
    size_t ret = floor(value / grid_height_);
    return ret > rows_ - 1 ? rows_ - 1 : ret;
  }
  size_t SafeColIndex(float value) const {
    if (value < 0) value = 0;
    size_t ret = floor(value / grid_width_);
    return ret > cols_ - 1 ? cols_ - 1 : ret;
  }

  std::vector<T>& data() { return data_; }

  const std::vector<T>& data() const { return data_; }

  size_t rows() const { return rows_; }
  size_t cols() const { return cols_; }

 private:
  std::vector<T> data_;
  size_t width_;
  size_t height_;
  size_t grid_width_;
  size_t grid_height_;
  size_t rows_;
  size_t cols_;
};

class KeyFrame {
 public:
  struct StereoMatch {
    CameraId master_id;
    CameraId slave_id;
    std::vector<int> s2m_matches;
    std::vector<float> s2m_distances;
    std::vector<int> m2s_matches;
    int GetMasterFeatIdx(size_t slave_feat_id) { return s2m_matches[slave_feat_id]; }
    int GetSlaveFeatIdx(size_t master_feat_id) { return m2s_matches[master_feat_id]; }
    size_t size() const {
      size_t cnt = 0;
      for (auto& m : s2m_matches) {
        cnt += m != -1;
      }
      return cnt;
    }
    void ResetStereoMatchByMasterFeatIdx(size_t master_feat_id) {
      int slave_feat_id = m2s_matches[master_feat_id];
      s2m_distances[slave_feat_id] = 10000.0;
      s2m_matches[slave_feat_id] = -1;
      m2s_matches[master_feat_id] = -1;
    }
  };

  struct FeatIndex {
    FeatIndex() {}
    FeatIndex(CameraId _cam_id, size_t _feat_idx) : cam_id(_cam_id), feat_idx(_feat_idx) {}
    CameraId cam_id;
    size_t feat_idx;
  };

  struct WeightedKf {
    WeightedKf() {}
    explicit WeightedKf(KeyFrame* _kf) : kf(_kf) {}
    WeightedKf(int _weight, KeyFrame* _kf) : weight(_weight), kf(_kf) {}
    WeightedKf(KeyFrame* _kf, int _weight) : kf(_kf), weight(_weight) {}
    bool operator>(const WeightedKf& other) const {
      if (weight > other.weight) return true;
      if (weight < other.weight) return false;
      return kf->frame_id_ < other.kf->frame_id_;
    }

    bool operator<(const WeightedKf& other) const {
      if (kf->frame_id_ < other.kf->frame_id_) return true;
      return false;
    }

    int weight = 0;
    KeyFrame* kf = nullptr;
  };

  explicit KeyFrame(const FrameMeta::Ptr& _frame_meta);
  KeyFrame(const FrameMeta::Ptr& frame_meta, FrameId frame_id, const cv::Mat& mask);

  // todo: check this function
  KeyFrame(const KeyFrame &_kf);

  ~KeyFrame() {}

  CameraId master_cam_id() const {
    for (auto id2s : is_master_) {
      if (id2s.second) return id2s.first;
    }
    return INVALID_CAM_ID;
  }

  CameraId slave_cam_id() const {
    auto _master_id = master_cam_id();
    if (_master_id == INVALID_CAM_ID) return INVALID_CAM_ID;
    for (auto id2s : is_master_) {
      if (!id2s.second && owner_cam_id_.at(id2s.first) == _master_id) {
        return id2s.first;
      }
    }
    return INVALID_CAM_ID;
  }

  std::vector<CameraId> slave_ids() const {
    std::vector<CameraId> slave_cameras;
    for (auto& id2state : is_master_) {
      if (!id2state.second && owner_cam_id_.at(id2state.first) != INVALID_CAM_ID) {
        slave_cameras.push_back(id2state.first);
      }
    }
    return slave_cameras;
  }

  int Octave(CameraId camera_id, size_t feat_idx) {
    int oct = key_points_[camera_id][feat_idx].octave;
    return oct;
  }

  float LevelFactor(CameraId camera_id, size_t feat_idx) {
    return pow(1.2, Octave(camera_id, feat_idx));
  }

  float LevelSigma2(CameraId camera_id, size_t feat_idx) {
    float v = LevelFactor(camera_id, feat_idx);
    return v * v;
  }

  CameraRole GetCameraRole(CameraId id) const {
    if (owner_cam_id_.count(id))
      return CameraRole::kSlave;
    else if (is_master_.count(id))
      return CameraRole::kMaster;
    return CameraRole::kStandalone;
  }

  CameraId GetSlaveCamera(CameraId master_id) const {
    for (auto id2s : is_master_) {
      if (!id2s.second && owner_cam_id_.at(id2s.first) == master_id) {
        return id2s.first;
      }
    }
    return INVALID_CAM_ID;
  }

  CameraId GetMasterCamera(CameraId slave_id) const {
    if (owner_cam_id_.count(slave_id)) return owner_cam_id_.at(slave_id);
    return INVALID_CAM_ID;
  }

  Timestamp timestamp() const { return timestamp_; }
  Timestamp raw_timestamp() const { return raw_timestamp_; }
  FrameId frame_id() const { return frame_id_; }

  const std::vector<MapPoint*>& map_points(CameraId cam_id) const { return map_points_.at(cam_id); }
  std::vector<MapPoint*>& map_points(CameraId cam_id) { return map_points_[cam_id]; }

  const std::map<CameraId, std::vector<MapPoint*>>& map_points() const { return map_points_; }

  size_t map_point_size() {
    size_t cnt = 0;
    for (auto cam2mpts : map_points_) {
      for (auto& mpt : cam2mpts.second) {
        if (mpt != nullptr) cnt += 1;
      }
    }
    return cnt;
  }

  void AddMapPoint(MapPoint* map_point, CameraId cam_id, const size_t idx,
                   const std::string& tag = "") {
    CHECK_EQ(map_points_[cam_id][idx], nullptr)
        << tag << " frm id " << frame_id_ << " cam & idx " << cam_id << " " << idx;
    map_points_[cam_id][idx] = map_point;
  }

  std::vector<FeatIndex> AddMapPoint(MapPoint* map_point, CameraId cam_id, const size_t idx,
                                     bool try_association, bool check_reproj, bool add_ob,
                                     const std::string& tag);

  void ReplaceMapPointMatch(MapPoint* map_point, CameraId cam_id, const size_t& idx) {
    map_points_[cam_id][idx] = map_point;
  }

  void EraseMapPointMatch(CameraId cam_id, size_t idx);

  void EraseMapPointMatchSplit(CameraId cam_id, size_t idx);

  std::vector<size_t> GetFeatureInArea(Eigen::Vector2d xy, float radius, int minLevel = -1,
                                       int maxLevel = 100);

  void UpdateConnections();
  void AddConnection(KeyFrame* kf, int weight);
  void EraseConnection(KeyFrame* kf);
  std::vector<KeyFrame*> GetBestCovisibilityKeyFrames(int N = -1);
  std::vector<WeightedKf> GetBestCovisibilityKeyFramesWithWeight(int N = -1);

  std::vector<WeightedKf> GetPreBestCovisibilityKeyFramesWithWeight(int N = -1);

  const std::map<KeyFrame*, int, KeyFrameCmp>& GetConnectKeyFrameWeights();
  void AddChild(KeyFrame* kf, int weight);
  void EraseChild(KeyFrame* kf);
  void ChangeParent(KeyFrame* kf, int weight);
  std::set<WeightedKf> GetChilds() { return children_; }
  WeightedKf GetParent() { return parent_; }

  const StereoMatch& Slave2MasterMatches(CameraId slave_id) { return s2m_matches_[slave_id]; }

  bool HaveStereoMatches(CameraId slave_id) { return s2m_matches_.count(slave_id); }

  MapPoint* GetMapPoint(CameraId cam_id, size_t feat_id);

  dm::math::Rigid3d camera_pose(CameraId cam_id) const {
    return RigidFromVector(pose_) * camera_extrinsics_[cam_id];
  }

  dm::math::Rigid3d pose() const { return RigidFromVector(pose_); }

  Eigen::Vector6d pose_vec() const { return pose_; }

  void set_pose(const dm::math::Rigid3d& pose) { pose_ = RigidToVector(pose); }

  StereoMatch MatchFeatures(CameraId master_id, CameraId slave_id);

  double* pose_data() { return pose_.data(); }

  const double* pose_data() const { return pose_.data(); }

  void Analyze(const std::string& tag);

  ImgGrid<std::vector<size_t>>& grid() {
    return grid_;
  }

  const cv::Mat& descriptor(CameraId cam_id) const { return descriptors_.at(cam_id); }

  FrameMeta::Ptr frame_meta() const { return frame_meta_; }

  size_t FeatureSize(CameraId cam_id) { return key_points_[cam_id].size(); }

  const DBoW3::BowVector& bow_vecs(CameraId cam_id) const { return bow_vecs_.at(cam_id); }

  const DBoW3::FeatureVector& feat_vecs(CameraId cam_id) const { return feat_vecs_.at(cam_id); }

  const std::vector<cv::KeyPoint>& key_points(CameraId cam_id) const {
    return key_points_.at(cam_id);
  }

  const std::vector<Eigen::Vector2d>& key_points_undistorted(CameraId cam_id) const {
    return key_points_undistorted_.at(cam_id);
  }

  // call this function before remove kf from map
  void Reset();

  dm::math::Rigid3d odo_pose() { return odo_pose_; }

  cv::Mat DrawOpticalMatches();

  std::vector<cv::DMatch> StereoMatchByOF(CameraId query_cam, CameraId train_cam);

  static void InitCameraParameters(const std::vector<dm::common::ImageUndistortion>& intrinsics,
                                   const std::vector<dm::math::Rigid3d>& extrinsics) {
    std::lock_guard<std::shared_mutex> lg(params_mutex_);
    image_intrinsics_ = intrinsics;
    camera_extrinsics_ = extrinsics;
  }

  static void UpdateCameraIntrinsics(const std::vector<dm::common::ImageUndistortion>& intrinsics) {
    std::lock_guard<std::shared_mutex> lg(params_mutex_);
    for (size_t i = 0; i < intrinsics.size(); ++i) {
      image_intrinsics_[i].Update(intrinsics[i].intrinsic(),
                                  intrinsics[i].distortion_coefficients());
    }
  }

  static void InitOrbExtractor() { return; }

  static void InitVocabulary(const std::string& path) {
    voc_ = std::make_shared<DBoW3::Vocabulary>(path);
  }

  static void InitVocabulary(std::shared_ptr<DBoW3::Vocabulary> voc) { voc_ = voc; }

  static std::shared_ptr<DBoW3::Vocabulary> voc() { return voc_; }

  static dm::math::Rigid3d Extrinsic(CameraId cam_id) {
    std::shared_lock<std::shared_mutex> sl(params_mutex_);
    return camera_extrinsics_[cam_id];
  }

  static dm::common::ImageUndistortion Intrinsic(CameraId cam_id) {
    std::shared_lock<std::shared_mutex> sl(params_mutex_);
    return image_intrinsics_[cam_id];
  }

  static double InverseF2() {
    CHECK_GE(image_intrinsics_.size(), 1);
    std::lock_guard<std::shared_mutex> sl(params_mutex_);
    double inverse_f = 1.0 / image_intrinsics_[0].f();
    return inverse_f * inverse_f;
  }

  static std::vector<dm::common::ImageUndistortion> Intrinsics() {
    std::shared_lock<std::shared_mutex> sl(params_mutex_);
    return image_intrinsics_;
  }

  static std::vector<dm::math::Rigid3d> Extrinsics() {
    std::shared_lock<std::shared_mutex> sl(params_mutex_);
    return camera_extrinsics_;
  }

  std::vector<FeatIndex> GetStereoAssociation(CameraId cam_id, size_t feat_idx) {
    std::vector<FeatIndex> result;
    for (const auto& id_s2m : s2m_matches_) {
      const auto& s2m = id_s2m.second;
      if (s2m.master_id == cam_id) {
        auto ass = s2m.m2s_matches[feat_idx];
        if (ass != -1) result.emplace_back(s2m.slave_id, ass);
      }
      if (s2m.slave_id == cam_id) {
        auto ass = s2m.s2m_matches[feat_idx];
        if (ass != -1) result.emplace_back(s2m.master_id, ass);
      }
    }
    return result;
  }

  void EraseStereoAssociation(CameraId first_cam, CameraId second_cam, size_t first_feat_idx) {
    for (auto& id_s2m : s2m_matches_) {
      auto& s2m = id_s2m.second;
      if (s2m.master_id == first_cam && s2m.slave_id == second_cam) {
        auto slave_idx = s2m.m2s_matches[first_feat_idx];
        s2m.m2s_matches[first_feat_idx] = -1;
        if (slave_idx != -1) s2m.s2m_matches[slave_idx] = -1;

      } else if (s2m.master_id == second_cam && s2m.slave_id == first_cam) {
        auto master_idx = s2m.s2m_matches[first_feat_idx];
        s2m.s2m_matches[first_feat_idx] = -1;
        if (master_idx != -1) s2m.m2s_matches[master_idx] = -1;
      }
    }
  }

  void EraseLoopInfo(FrameId loop_frm_id) {
    auto tmp = LoopInfo(loop_frm_id);
    if (loop_infos_.count(tmp)) loop_infos_.erase(tmp);
  }

  void AddLoopInfo(const LoopInfo &info) {
    loop_infos_.insert(info);
  }

  const std::set<LoopInfo> &loop_infos() const {
    return loop_infos_;
  }

  void set_stable_pose(const Eigen::Vector6d& pose) {
    std::lock_guard<std::mutex> lg(stable_pose_mutex_);
    stable_pose_ = pose;
  }

  void set_stable_pose(const dm::math::Rigid3d& pose) {
    std::lock_guard<std::mutex> lg(stable_pose_mutex_);
    stable_pose_ = RigidToVector(pose);
  }

  Eigen::Vector6d stable_pose() {
    std::lock_guard<std::mutex> lg(stable_pose_mutex_);
    return stable_pose_;
  }

  const std::map<KeyFrame*, int, KeyFrameCmp>& connected_kf_weights() {
    return connected_kf_weights_;
  }

 public:
  std::atomic_bool forced_retention_ = false;
  bool localization_anchor_ = false;

  Timestamp timestamp_;
  Timestamp raw_timestamp_;
  FrameId frame_id_;

  Eigen::Vector6d pose_;  // Twi

  std::map<CameraId, std::vector<cv::KeyPoint>> key_points_;
  std::map<CameraId, std::vector<Eigen::Vector2d>> key_points_undistorted_;
  std::map<CameraId, cv::Mat> descriptors_;
  std::map<CameraId, StereoMatch> s2m_matches_;

  // Grid over the master image to speed up feature matching, index: grid[row][col]
  ImgGrid<std::vector<size_t>> grid_;

  std::map<CameraId, std::vector<MapPoint*>> map_points_;

  std::map<CameraId, DBoW3::BowVector> bow_vecs_;
  std::map<CameraId, DBoW3::FeatureVector> feat_vecs_;

  std::map<CameraId, bool> is_master_;

  std::map<CameraId, CameraId> owner_cam_id_;

  // relative to projection
  std::map<uint32_t, cv::Point2f> project_points_;

  // consistent with ordered_connected_kfs_
  std::map<KeyFrame*, int, KeyFrameCmp> connected_kf_weights_;
  std::set<WeightedKf, std::greater<WeightedKf>> ordered_connected_kfs_;

  // Spanning Tree
  WeightedKf parent_;
  std::set<WeightedKf> children_;

  dm::math::Rigid3d odo_pose_;

  std::mutex stable_pose_mutex_;
  Eigen::Vector6d stable_pose_;

  FrameMeta::Ptr frame_meta_ = nullptr;

  std::set<LoopInfo> loop_infos_;

  static FrameId public_self_inc_id_;
  static std::shared_ptr<ORBextractor> orb_extractor_;
  static std::shared_ptr<DBoW3::Vocabulary> voc_;
  static const int kStereoMatchDistanceThreshold;

 private:
  static std::shared_mutex params_mutex_;
  static std::vector<dm::common::ImageUndistortion> image_intrinsics_;
  static std::vector<dm::math::Rigid3d> camera_extrinsics_;
};

template <typename T>
std::vector<cv::KeyPoint> pts2kpts(std::vector<T> pts) {
  std::vector<cv::KeyPoint> kpts0;
  for (int i = 0; i < pts.size(); i++) {
    kpts0.push_back(cv::KeyPoint(cv::Point2f(pts[i].x(), pts[i].y()), 0));
  }
  return kpts0;
}

// compute projection error, unit:pixel
double ComputeProjectError(const Eigen::Vector3d& wpt, KeyFrame* kf, CameraId cam_id,
                           uint32_t feat_id, double invalid_err = 10000.0);

double ComputeProjectError(MapPoint* pt, KeyFrame* kf, CameraId cam_id, uint32_t feat_id,
                           double invalid_err = 10000.0);

}  // namespace dm::vio::lm

#endif  // VIO_MAP_KEY_FRAME_H_
