// Copyright 2020 DeepMirror Inc. All rights reserved.
#include "vio/map/key_frame.h"
#include <algorithm>
#include <map>
#include <memory>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>
#include "common/timer.h"
#include "vio/map/map_point.h"
#include "vio/map/orb_matcher.h"

namespace dm::vio::lm {

FrameId KeyFrame::public_self_inc_id_ = 0;
std::shared_ptr<ORBextractor> KeyFrame::orb_extractor_ =
    std::make_shared<ORBextractor>(800, 1.2, 8, 20, 7);
std::shared_ptr<DBoW3::Vocabulary> KeyFrame::voc_ = nullptr;
const int KeyFrame::kStereoMatchDistanceThreshold = 70;
std::shared_mutex KeyFrame::params_mutex_;
std::vector<dm::common::ImageUndistortion> KeyFrame::image_intrinsics_;
std::vector<dm::math::Rigid3d> KeyFrame::camera_extrinsics_;

bool KeyFrameCmp::operator()(const KeyFrame* kf1, const KeyFrame* kf2) const {
  return kf1->frame_id() < kf2->frame_id();
}

KeyFrame::KeyFrame(const FrameMeta::Ptr& frame_meta)
    : KeyFrame(frame_meta, public_self_inc_id_++, cv::Mat()) {}

KeyFrame::KeyFrame(const FrameMeta::Ptr& frame_meta, FrameId frame_id, const cv::Mat& mask) {
  frame_id_ = frame_id;
  pose_ = RigidToVector(frame_meta->odo_pose);
  stable_pose_ = pose_;
  odo_pose_ = frame_meta->odo_pose;
  timestamp_ = frame_meta->timestamp;
  raw_timestamp_ = frame_meta->raw_timestamp;
  is_master_ = frame_meta->is_master;
  owner_cam_id_ = frame_meta->owner_cam_id;
  frame_meta_ = frame_meta;
  CameraId master_cam_id = this->master_cam_id();
  VLOG(1) << "Keyframe construct, master id is " << master_cam_id << " kf_id: " << frame_id_;

  // extract orb features
  for (const auto& cam_id2img : frame_meta->images) {
    auto cam_id = cam_id2img.first;
    auto img = cam_id2img.second;
    std::vector<cv::KeyPoint> keypoints;
    const auto& pixels = frame_meta->feature_pixels.at(cam_id);
    cv::Mat descriptors = frame_meta->feature_descriptors.at(cam_id);
    for (int i=0; i<pixels.rows; ++i)
      keypoints.emplace_back(cv::Point2f(pixels.at<float>(i,0),pixels.at<float>(i,1)), 1.f, 0.f, 1.f, 0);
    // A normalized image signature retrieves candidates; geometry verifies them.
    cv::Mat signature;
    if (!descriptors.empty()) {
      cv::reduce(descriptors, signature, 0, cv::REDUCE_AVG);
      signature /= std::max(cv::norm(signature, cv::NORM_L2), 1e-12);
    }
    auto& bow=bow_vecs_[cam_id];
    for(int j=0; j<signature.cols; ++j) bow[j]=signature.at<float>(0,j);
    auto& indices=feat_vecs_[cam_id][0];
    for(unsigned int i=0; i<keypoints.size(); ++i) indices.push_back(i);

    key_points_[cam_id] = std::move(keypoints);
    key_points_undistorted_[cam_id].resize(key_points_[cam_id].size());
    descriptors_[cam_id] = descriptors;
    map_points_[cam_id] = std::vector<MapPoint*>(key_points_[cam_id].size(), nullptr);

    if (cam_id == master_cam_id) {
      grid_ = ImgGrid<std::vector<size_t>>(image_intrinsics_.at(cam_id).width(),
                                           image_intrinsics_.at(cam_id).height(), 15, 15);
      const std::vector<cv::KeyPoint>& kpts = key_points_[master_cam_id];
      for (size_t index = 0; index < kpts.size(); ++index) {
        grid_(kpts[index].pt.x, kpts[index].pt.y).push_back(index);
      }

      for (auto& ele : grid_.data()) {
        std::sort(ele.begin(), ele.end(), [&](size_t x, size_t y) {
          if (kpts[x].response > kpts[y].response) return true;
          if (kpts[x].response < kpts[y].response) return false;
          return x < y;
        });
      }
    }

    std::vector<Eigen::Vector2d> distorted_points;
    distorted_points.reserve(key_points_[cam_id].size());
    for (const auto& kp : key_points_[cam_id]) {
      distorted_points.emplace_back(kp.pt.x, kp.pt.y);
    }
    std::shared_lock<std::shared_mutex> sl(params_mutex_);
    key_points_undistorted_[cam_id] =
        image_intrinsics_.at(cam_id).LiftPointsToRays(distorted_points);
  }

  // do stereo match between master and slave image
  auto slave_cams = slave_ids();
  for (const auto& s_id : slave_cams) {
    auto m_id = GetMasterCamera(s_id);
    CHECK_NE(m_id, INVALID_CAM_ID);
    auto match_result = MatchFeatures(m_id, s_id);
    s2m_matches_[s_id] = match_result;
  }
}

KeyFrame::KeyFrame(const KeyFrame& other) {
  timestamp_ = other.timestamp_;
  raw_timestamp_ = other.raw_timestamp_;
  frame_id_ = other.frame_id_;

  pose_ = other.pose_;

  key_points_ = other.key_points_;
  key_points_undistorted_ = other.key_points_undistorted_;
  descriptors_ = other.descriptors_;

  s2m_matches_ = other.s2m_matches_;

  grid_ = other.grid_;

  map_points_ = other.map_points_;

  bow_vecs_ = other.bow_vecs_;
  feat_vecs_ = other.feat_vecs_;

  is_master_ = other.is_master_;
  owner_cam_id_ = other.owner_cam_id_;

  project_points_ = other.project_points_;

  connected_kf_weights_ = other.connected_kf_weights_;
  ordered_connected_kfs_ = other.ordered_connected_kfs_;

  parent_ = other.parent_;
  children_ = other.children_;

  odo_pose_ = other.odo_pose_;
}


KeyFrame::StereoMatch KeyFrame::MatchFeatures(CameraId master_id, CameraId slave_id) {
  // SuperGlue supplies ordered, subpixel-refined stereo correspondences.
  std::vector<cv::DMatch> matches;
  for(int i=0; i<descriptors_[master_id].rows; ++i) matches.emplace_back(i,i,0.f);

  std::vector<int> s2m_matches(descriptors_[slave_id].rows, -1);
  std::vector<float> s2m_distances(descriptors_[slave_id].rows, 10000.0);
  std::vector<int> m2s_matches(descriptors_[master_id].rows, -1);

  for (const auto& match : matches) {
    if (match.distance >= kStereoMatchDistanceThreshold || s2m_matches[match.trainIdx] != -1 ||
        m2s_matches[match.queryIdx] != -1) {
      continue;
    }

    CHECK_EQ(s2m_matches[match.trainIdx], -1);
    CHECK_EQ(m2s_matches[match.queryIdx], -1);

    auto m = match.queryIdx;
    auto s = match.trainIdx;
    s2m_matches[s] = m;
    m2s_matches[m] = s;
    s2m_distances[s] = match.distance;
  }

  KeyFrame::StereoMatch stereo_match = {master_id, slave_id, s2m_matches, s2m_distances,
                                        m2s_matches};
  return stereo_match;
}

std::vector<KeyFrame*> KeyFrame::GetBestCovisibilityKeyFrames(int N) {
  std::vector<KeyFrame*> kfs;
  size_t limit = ordered_connected_kfs_.size();
  if (N > 0 && N < limit) limit = N;
  size_t cnt = 0;
  for (const auto& w2kf : ordered_connected_kfs_) {
    kfs.push_back(w2kf.kf);
    if (++cnt >= limit) break;
  }
  return kfs;
}

std::vector<KeyFrame::WeightedKf> KeyFrame::GetBestCovisibilityKeyFramesWithWeight(int N) {
  std::vector<WeightedKf> result;
  size_t limit = ordered_connected_kfs_.size();
  if (N > 0 && N < limit) limit = N;
  size_t cnt = 0;
  for (const auto& w2kf : ordered_connected_kfs_) {
    result.push_back(w2kf);
    if (++cnt >= limit) break;
  }
  return result;
}
std::vector<KeyFrame::WeightedKf> KeyFrame::GetPreBestCovisibilityKeyFramesWithWeight(int N) {
  std::vector<WeightedKf> result;
  size_t limit = ordered_connected_kfs_.size();
  if (N > 0 && N < limit) limit = N;
  size_t cnt = 0;
  for (auto w2kf : ordered_connected_kfs_) {
    if (w2kf.kf->frame_id() > frame_id_) continue;
    result.push_back(w2kf);
    if (++cnt >= limit) break;
  }
  return result;
}

const std::map<KeyFrame*, int, KeyFrameCmp>& KeyFrame::GetConnectKeyFrameWeights() {
  return connected_kf_weights_;
}

void KeyFrame::ChangeParent(KeyFrame* kf, int weight) {
  CHECK(kf != this) << "ERROR: Change parent KF, the parent and child are the same KF";
  parent_ = {weight, kf};
  kf->AddChild(this, weight);
}

/**
 * @brief the uv input must pass IsInFrustum(), meaning that the uv is validate
 *
 * @param uv input image coordinate
 * @param radius search raduis
 * @return std::vector<size_t> keypoint indices in search area
 */
std::vector<size_t> KeyFrame::GetFeatureInArea(Eigen::Vector2d uv, float radius, int minLevel,
                                               int maxLevel) {
  CameraId master = master_cam_id();
  std::vector<size_t> indices;
  int search_grid_x_min = grid_.SafeColIndex(uv(0) - radius);
  int search_grid_x_max = grid_.SafeColIndex(uv(0) + radius);
  int search_grid_y_min = grid_.SafeRowIndex(uv(1) - radius);
  int search_grid_y_max = grid_.SafeRowIndex(uv(1) + radius);
  for (int x = search_grid_x_min; x <= search_grid_x_max; ++x) {
    for (int y = search_grid_y_min; y <= search_grid_y_max; ++y) {
      const std::vector<size_t> feat_in_cell = grid_.at(y, x);
      for (const auto& idx : feat_in_cell) {
        cv::KeyPoint kpt = key_points_[master][idx];
        if (kpt.octave < minLevel || kpt.octave > maxLevel) continue;
        Eigen::Vector2d err = {kpt.pt.x - uv(0), kpt.pt.y - uv(1)};
        if (err.norm() < radius) {
          indices.push_back(idx);
        }
      }
    }
  }
  return indices;
}

void KeyFrame::UpdateConnections() {
  // Delete all connections associated with this kf
  for (const auto& kf_weight : connected_kf_weights_) {
    kf_weight.first->EraseConnection(this);
  }
  connected_kf_weights_.clear();
  ordered_connected_kfs_.clear();

  std::map<KeyFrame*, int, KeyFrameCmp> kf2counter;
  CameraId master = master_cam_id();

  // For all map points in keyframe check in which other keyframes are they seen
  // Increase counter for those keyframes
  for (const auto& mpt : map_points(master)) {
    if (!mpt || mpt->IsBad()) continue;
    for (const auto& it : mpt->observations()) {
      if (it.first->frame_id() == frame_id_) continue;
      kf2counter[it.first]++;
    }
  }
  WeightedKf max_w_kf(nullptr, -1);
  int th = 15;
  for (const auto& it : kf2counter) {
    if (it.second > max_w_kf.weight) {
      max_w_kf = WeightedKf(it.first, it.second);
    }
    if (it.second >= th) {
      ordered_connected_kfs_.insert(WeightedKf(it.second, it.first));
      it.first->AddConnection(this, it.second);
    }
  }
  if (ordered_connected_kfs_.empty() && max_w_kf.kf != nullptr) {
    ordered_connected_kfs_.insert(max_w_kf);
  }
  for (const auto& weighted_kf : ordered_connected_kfs_) {
    connected_kf_weights_[weighted_kf.kf] = weighted_kf.weight;
    weighted_kf.kf->AddConnection(this, weighted_kf.weight);
  }
}

void KeyFrame::AddChild(KeyFrame* kf, int weight) { children_.insert(WeightedKf(kf, weight)); }

void KeyFrame::EraseChild(KeyFrame* kf) { children_.erase(WeightedKf(kf)); }

void KeyFrame::AddConnection(KeyFrame* kf, int weight) {
  if (connected_kf_weights_.count(kf)) {
    if (weight == connected_kf_weights_[kf]) {
      return;
    } else {
      EraseConnection(kf);
    }
  }
  connected_kf_weights_[kf] = weight;
  ordered_connected_kfs_.insert(WeightedKf(weight, kf));
}

void KeyFrame::EraseConnection(KeyFrame* kf) {
  if (!connected_kf_weights_.count(kf)) return;
  connected_kf_weights_.erase(kf);
  bool has_erase = false;
  for (auto it = ordered_connected_kfs_.begin(); it != ordered_connected_kfs_.end();) {
    if (it->kf == kf) {
      it = ordered_connected_kfs_.erase(it);
      has_erase = true;
      break;
    } else {
      it++;
    }
  }
  CHECK(has_erase) << "connected_kf_weights_ is not consistent with ordered_connected_kfs_ for kf "
                   << frame_id_ << "( " << kf->frame_id_ << " )";
}

void KeyFrame::EraseMapPointMatch(CameraId cam_id, size_t idx) {
  auto mpt = map_points_[cam_id][idx];
  map_points_[cam_id][idx] = nullptr;
  std::vector<FeatIndex> feat_indices = GetStereoAssociation(cam_id, idx);
  for (const auto& feat_index : feat_indices) {
    if (!map_points_[feat_index.cam_id][feat_index.feat_idx]) {
      continue;
    }
    if (map_points_[feat_index.cam_id][feat_index.feat_idx] != mpt) {
      LOG(WARNING) << "map point is different for a stereo match";
    } else {
      map_points_[feat_index.cam_id][feat_index.feat_idx] = nullptr;
    }
  }
}

void KeyFrame::EraseMapPointMatchSplit(CameraId cam_id, size_t idx) {
  auto mpt = map_points_[cam_id][idx];
  map_points_[cam_id][idx] = nullptr;
  std::vector<FeatIndex> feat_indices = GetStereoAssociation(cam_id, idx);
  for (const auto& feat_index : feat_indices) {
    if (!map_points_[feat_index.cam_id][feat_index.feat_idx]) {
      continue;
    }
    if (map_points_[feat_index.cam_id][feat_index.feat_idx] != mpt) {
      LOG(WARNING) << "map point is different for a stereo match";
      continue;
    }
    this->EraseStereoAssociation(cam_id, feat_index.cam_id, idx);
  }
}

MapPoint* KeyFrame::GetMapPoint(CameraId cam_id, size_t feat_id) {
  return map_points_[cam_id][feat_id];
}

void KeyFrame::Reset() {
  // Delete all connections associated with this kf
  for (const auto& kf_weight : connected_kf_weights_) {
    kf_weight.first->EraseConnection(this);
  }
  connected_kf_weights_.clear();
  ordered_connected_kfs_.clear();
  for (const auto& cam_id2mpts : map_points_) {
    std::vector<MapPoint*> pts = cam_id2mpts.second;
    for (const auto& mpt : cam_id2mpts.second) {
      if (!mpt) continue;
      mpt->EraseObservation(this);
    }
  }
  map_points_.clear();

  // update map spanning tree
  std::unordered_set<KeyFrame*> parent_candidates;
  if (parent_.kf) {
    parent_candidates.insert(parent_.kf);
  }
  // Assign at each iteration one child with a parent (the pair with highest covisibility weight)
  // Include that children as new parent candidate for the rest
  while (!children_.empty()) {
    bool continue_to_assign = false;

    int max_weight = -1;
    KeyFrame* child_ptr = nullptr;
    KeyFrame* parent_ptr = nullptr;

    for (const auto& child : children_) {
      std::map<KeyFrame*, int, KeyFrameCmp> kf_weights = child.kf->GetConnectKeyFrameWeights();
      // Check if a parent candidate is connected to the keyframe && find the pair with highest
      // covisibility weight
      for (const auto& candidate_parent : parent_candidates) {
        if (kf_weights.count(candidate_parent) && kf_weights[candidate_parent] > max_weight) {
          child_ptr = child.kf;
          parent_ptr = candidate_parent;
          max_weight = kf_weights[candidate_parent];
          continue_to_assign = true;
        }
      }
    }
    if (continue_to_assign) {
      child_ptr->ChangeParent(parent_ptr, max_weight);
      parent_candidates.insert(child_ptr);
      children_.erase(WeightedKf(child_ptr));
    } else {
      break;
    }
  }
  // If a children has no covisibility links with any parent candidate, assign to the original
  // parent of this KF
  if (!children_.empty()) {
    for (const auto& child : children_) {
      child.kf->ChangeParent(parent_.kf, 0);
    }
  }
  children_.clear();

  if (parent_.kf) {
    parent_.kf->EraseChild(this);
    parent_ = WeightedKf(nullptr);
  }
}

cv::Mat KeyFrame::DrawOpticalMatches() {
  auto frm_meta = frame_meta_;
  auto m_id = master_cam_id();
  auto s_id = slave_cam_id();
  auto uvs0 = frm_meta->uvs[m_id];
  auto uvs1 = frm_meta->uvs[s_id];

  std::vector<cv::KeyPoint> kpts0 = pts2kpts(uvs0), kpts1 = pts2kpts(uvs1);
  std::map<FeatureId, std::vector<size_t>> feat2uv_ids;
  for (size_t i = 0; i < frm_meta->feature_observations[m_id].size(); i++) {
    auto feat_id = frm_meta->feature_observations[m_id][i];
    feat2uv_ids[feat_id].push_back(i);
  }
  for (size_t i = 0; i < frm_meta->feature_observations[s_id].size(); i++) {
    auto feat_id = frm_meta->feature_observations[s_id][i];
    feat2uv_ids[feat_id].push_back(i);
  }

  std::vector<cv::DMatch> matches;
  for (const auto& f2uv : feat2uv_ids) {
    auto uv_ids = f2uv.second;
    if (uv_ids.size() < 2) continue;
    matches.push_back(cv::DMatch(uv_ids[m_id], uv_ids[s_id], 0));
  }

  cv::Mat out_img;
  cv::drawMatches(frame_meta_->images[m_id], kpts0, frame_meta_->images[s_id], kpts1, matches,
                  out_img);

  return out_img;
}

std::vector<cv::DMatch> KeyFrame::StereoMatchByOF(CameraId query_cam, CameraId train_cam) {
  auto frm_meta = frame_meta_;
  auto uvs0 = frm_meta->uvs[query_cam];
  auto uvs1 = frm_meta->uvs[train_cam];
  dm::math::Rigid3d Ttq = KeyFrame::Extrinsic(train_cam).Inverse() * KeyFrame::Extrinsic(query_cam);
  Eigen::Matrix3d E = Ttq.EssentialMatrix();
  std::vector<cv::DMatch> final_matches;

  std::unordered_map<size_t, size_t> of_matches;

  std::map<FeatureId, std::vector<size_t>> feat2uv_ids;
  for (size_t i = 0; i < frm_meta->feature_observations[query_cam].size(); i++) {
    auto feat_id = frm_meta->feature_observations[query_cam][i];
    feat2uv_ids[feat_id].push_back(i);
  }
  for (size_t i = 0; i < frm_meta->feature_observations[train_cam].size(); i++) {
    auto feat_id = frm_meta->feature_observations[train_cam][i];
    feat2uv_ids[feat_id].push_back(i);
  }
  for (const auto& f2uv : feat2uv_ids) {
    auto uv_ids = f2uv.second;
    if (uv_ids.size() < 2) continue;
    of_matches[uv_ids[0]] = uv_ids[1];
  }

  auto query_intrinsic = KeyFrame::Intrinsic(query_cam);
  auto train_intrinsic = KeyFrame::Intrinsic(train_cam);
  ImgGrid<std::vector<size_t>> query_grid_indices(query_intrinsic.width(), query_intrinsic.height(),
                                                  30, 30);
  ImgGrid<std::vector<size_t>> orb_train_grid_indices(train_intrinsic.width(),
                                                      train_intrinsic.height(), 30, 30);

  for (size_t i = 0; i < uvs0.size(); i++) {
    const auto& uv = uvs0[i];
    if (!of_matches.count(i)) continue;
    query_grid_indices(uv.x(), uv.y()).push_back(i);
  }
  for (size_t i = 0; i < key_points_[train_cam].size(); i++) {
    const auto& uv = key_points_[train_cam][i].pt;
    orb_train_grid_indices(uv.x, uv.y).push_back(i);
  }
  const float epipolar_th = 5.0 / train_intrinsic.f();

  std::vector<bool> used_train_indices(FeatureSize(train_cam), false);

  int orb_th = 100;

  for (size_t i = 0; i < descriptors_[query_cam].rows; i++) {
    auto orb = key_points_[query_cam][i];
    const auto& of_indices = query_grid_indices(orb.pt.x, orb.pt.y);
    size_t best_idx = UINT32_MAX;
    float best_dist = 100000;
    for (size_t j = 0; j < of_indices.size(); j++) {
      float dist = (uvs0[of_indices[j]] - Eigen::Vector2d(orb.pt.x, orb.pt.y)).norm();
      if (dist < best_dist) {
        best_dist = dist;
        best_idx = of_indices[j];
      }
    }
    if (best_idx == UINT32_MAX) continue;
    auto train_of_pt = uvs1[of_matches[best_idx]];

    int row = orb_train_grid_indices.RowIndex(train_of_pt.y());
    int col = orb_train_grid_indices.ColIndex(train_of_pt.x());

    size_t best_match = UINT32_MAX;
    size_t best_match_dist = 1000;
    for (int r = std::max(row - 1, 0); r < std::min(size_t(row + 1), orb_train_grid_indices.rows());
         r++) {
      for (int c = std::max(col - 1, 0);
           c < std::min(size_t(col + 1), orb_train_grid_indices.cols()); c++) {
        const auto& train_orb_indices = orb_train_grid_indices.at(r, c);
        for (const auto& idx : train_orb_indices) {
          if (used_train_indices[idx]) continue;
          auto train_kpt = key_points_[train_cam][idx];
          float dist = CompueEpipolarDist(key_points_undistorted_[query_cam][i],
                                          key_points_undistorted_[train_cam][idx], E);
          if (dist > epipolar_th) continue;
          int match_dist = ORBMatcher::DescriptorDistance(descriptors_[query_cam].row(i),
                                                          descriptors_[train_cam].row(idx));
          if (match_dist < orb_th || match_dist < best_match_dist) {
            best_match_dist = match_dist;
            best_match = idx;
          }
        }
      }
    }

    if (best_match == UINT32_MAX) continue;
    used_train_indices[best_match] = true;
    final_matches.push_back(cv::DMatch(i, best_match, best_match_dist));
  }

  return final_matches;
}

std::vector<KeyFrame::FeatIndex> KeyFrame::AddMapPoint(MapPoint* map_point, CameraId cam_id,
                                                       const size_t idx, bool try_association,
                                                       bool check_reproj, bool add_ob,
                                                       const std::string& tag) {
  CHECK_EQ(map_points_[cam_id][idx], nullptr)
      << tag << " frm id " << frame_id_ << " cam & idx " << cam_id << " " << idx;

  std::vector<KeyFrame::FeatIndex> observations;

  bool is_good = true;
  if (check_reproj) {
    float err = ComputeProjectError(map_point, this, cam_id, idx);
    if (err > 3.0 * LevelFactor(cam_id, idx)) is_good = false;
  }
  if (is_good) {
    map_points_[cam_id][idx] = map_point;
    observations.emplace_back(cam_id, idx);
  }
  if (try_association) {
    std::vector<FeatIndex> ass_feats = GetStereoAssociation(cam_id, idx);
    for (const auto& ass_feat : ass_feats) {
      if (map_points_[ass_feat.cam_id][ass_feat.feat_idx] != nullptr) {
        LOG(WARNING) << " AddMapPoint warning " << tag << " "
                     << "associated feature have map point!";
        EraseStereoAssociation(cam_id, ass_feat.cam_id, idx);
      }
      float err = ComputeProjectError(map_point, this, ass_feat.cam_id, ass_feat.feat_idx);
      if (err > 3.0 * LevelFactor(ass_feat.cam_id, ass_feat.feat_idx)) {
        EraseStereoAssociation(cam_id, ass_feat.cam_id, idx);
        continue;
      }
      map_points_[ass_feat.cam_id][ass_feat.feat_idx] = map_point;
      observations.emplace_back(ass_feat.cam_id, ass_feat.feat_idx);
    }
  }
  if (add_ob) {
    for (const auto& observation : observations) {
      map_point->AddObservation(this, observation.cam_id, observation.feat_idx);
    }
  }
  return observations;
}

double ComputeProjectError(const Eigen::Vector3d& wpt, KeyFrame* kf, CameraId cam_id,
                           uint32_t feat_id, double invalid_err) {
  dm::math::Rigid3d Twc = kf->camera_pose(cam_id);
  Eigen::Vector3d Pc = Twc.Inverse() * wpt;
  if (Pc[2] < 0.0 && invalid_err < 0)
    return Pc[2];
  else if (Pc[2] < 0.0)
    return invalid_err;
  Eigen::Vector2d xy_proj = Eigen::Vector2d(Pc[0] / Pc[2], Pc[1] / Pc[2]);
  Eigen::Vector2d xy_proj_distorted = KeyFrame::Intrinsic(cam_id).ProjectRayToPoint(xy_proj);
  cv::Point2f uv = kf->key_points(cam_id)[feat_id].pt;
  return (Eigen::Vector2d(uv.x, uv.y) - xy_proj_distorted).norm();
}

double ComputeProjectError(MapPoint* pt, KeyFrame* kf, CameraId cam_id, uint32_t feat_id,
                           double invalid_err) {
  return ComputeProjectError(pt->WorldPoint3d(), kf, cam_id, feat_id, invalid_err);
}

void KeyFrame::Analyze(const std::string& tag) {
  std::vector<size_t> ob_size(20, 0);
  for (const auto& mpt : map_points(master_cam_id())) {
    if (!mpt || mpt->IsBad()) continue;
    size_t num = mpt->NumOfObs();
    if (num > 20) num = 19;
    size_t idx = num;
    ob_size[idx]++;
  }
  std::stringstream ss;
  for (int i = 0; i < ob_size.size(); i++) {
    ss << "(" << i << ": " << ob_size[i] << ") ";
  }
  VLOG(2) << "KeyFrame map point ob size, " << tag << " : " << ss.str();
}

}  // namespace dm::vio::lm
