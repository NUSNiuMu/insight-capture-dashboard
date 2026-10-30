// Copyright 2020 DeepMirror Inc. All rights reserved.
#include "vio/map/map.h"
#include <set>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace dm::vio::lm {

int Map::KeyFramesSize() { return key_frames_.size(); }
bool Map::KeyFrameEmpty() { return key_frames_.empty(); }

int Map::MapPointsSize() { return map_points_.size(); }
bool Map::MapPointEmpty() { return map_points_.empty(); }

void Map::AddMapPoint(MapPoint* map_point) { map_points_[map_point->point_id()] = map_point; }

void Map::AddKeyFrame(KeyFrame* kf) {
  CHECK_NE(kf, nullptr);
  LOG(INFO) << "map add keyframe kf: " << kf->frame_id();
  std::lock_guard<std::mutex> kf_lg(kf_mutex_);
  {
    key_frames_[kf->frame_id()] = kf;
  }

}

void Map::EraseMapPoint(MapPoint* map_point) {
  if(map_point->replacement()) replacements_[map_point->point_id()]=*map_point->replacement();
  // map_point->SetBad();
  // return;
  // Remove observations of this map point in all keyframes
  auto kf_obs = map_point->observations();
  for (auto const& it : kf_obs) {
    KeyFrame* kf = it.first;
    std::set<MapPoint::Observation> obs = it.second;
    for (auto const& ob : obs) {
      kf->EraseMapPointMatch(ob.cam_id, ob.feat_id);
    }
  }
  map_points_.erase(map_point->point_id());
  delete map_point;
}

bool Map::EraseMapPointTS(MapPoint* map_point) {
  std::unique_lock<std::mutex> lock(mpt_mutex_, std::try_to_lock);
  if (lock.owns_lock()) {
    if (map_point->forced_retention()) {
      map_point->SetBad();
      delete_map_points_buffer_.insert(map_point);
      return false;
    } else {
      EraseMapPoint(map_point);
      return true;
    }
  } else {
    map_point->SetBad();
    delete_map_points_buffer_.insert(map_point);
  }
  return false;
}


void Map::EraseMapPoint(FeatureId mpt_id) {
  if (!map_points_.count(mpt_id)) return;
  auto map_point = map_points_[mpt_id];
  EraseMapPoint(map_point);
}

bool Map::EraseKeyFrameTS(KeyFrame* kf) {
  std::unique_lock<std::mutex> lock(kf_mutex_, std::try_to_lock);
  if (kf->frame_id() == 0 || kf->localization_anchor_) {
    return false;
  }
  if (lock.owns_lock()) {
    if (kf->forced_retention_) {
      delete_key_frames_buffer_.insert(kf);
      return false;
    } else {
      EraseKeyFrame(kf);
      return true;
    }
  } else {
    delete_key_frames_buffer_.insert(kf);
  }
  return false;
}

void Map::EraseKeyFrame(KeyFrame* kf) {
  if (kf->frame_id() == 0) return;
  kf->Reset();
  key_frames_.erase(kf->frame_id());
  delete kf;
}

std::vector<KeyFrame*> Map::GetBestCovisibilityKeyFramesTS(KeyFrame* query_kf, int n) {
  std::lock_guard<std::mutex> lg(kf_mutex_);
  std::vector<KeyFrame*> kfs = query_kf->GetBestCovisibilityKeyFrames(n);
  for (auto& kf : kfs) {
    kf->forced_retention_ = true;
  }
  return kfs;
}
namespace {
double AppearanceScore(const DBoW3::BowVector& a, const DBoW3::BowVector& b) {
  double score=0;
  for(const auto& entry:a) {
    auto found=b.find(entry.first);
    if(found!=b.end()) score+=entry.second*found->second;
  }
  return score;
}
}
std::vector<SimilarKfInfo> Map::FindSimilarFrames(KeyFrame* query, size_t max_size,
                                                int min_id_dist) {
  auto candidates=RetrieveSimilarFrames(query->bow_vecs(query->master_cam_id()), key_frames_.size());
  auto neighbors=query->GetBestCovisibilityKeyFrames();
  candidates.erase(std::remove_if(candidates.begin(),candidates.end(),[&](const auto& item) {
    return item.kf->frame_id()+min_id_dist>=query->frame_id() ||
      std::find(neighbors.begin(),neighbors.end(),item.kf)!=neighbors.end();
  }),candidates.end());
  if(candidates.size()>max_size) candidates.resize(max_size);
  return candidates;
}
std::vector<SimilarKfInfo> Map::RetrieveSimilarFrames(const DBoW3::BowVector& signature,
                                                    size_t max_size) const {
  std::vector<SimilarKfInfo> result;
  for(const auto& entry:key_frames_) {
    auto kf=entry.second;
    result.emplace_back(kf,AppearanceScore(signature,kf->bow_vecs(kf->master_cam_id())));
  }
  std::sort(result.begin(),result.end(),[](const auto& a,const auto& b) {
    return a.score!=b.score ? a.score>b.score : a.kf->frame_id()>b.kf->frame_id();
  });
  if(result.size()>max_size) result.resize(max_size);
  return result;
}

void Map::UpdateKeyFramePoses(const std::map<FrameId, Eigen::Vector6d>& new_kf_poses) {
  auto kf_begin = key_frames_.begin();
  auto kf_end = key_frames_.end();
  auto kf_it = kf_begin;

  auto new_kf_begin = new_kf_poses.begin();
  auto new_kf_end = new_kf_poses.end();
  auto new_it = new_kf_begin;

  std::unordered_map<KeyFrame*, dm::math::Rigid3d> Tn_os;  // T new_world_old_world
  dm::math::Rigid3d Tn_o, new_kf_pose;
  while (kf_it != kf_end && new_it != new_kf_end) {
    //    LOG(INFO)<<"UpdateKeyFramePoses kf ids: "<<kf_it->first<<" -- "<<new_it->first;
    if (kf_it->first < new_kf_begin->first) {
      kf_it++;
      continue;
    }
    auto old_kf_pose = kf_it->second->pose();
    if (kf_it->first == new_it->first) {
      new_kf_pose = RigidFromVector(new_it->second);
      Tn_o = new_kf_pose * old_kf_pose.Inverse();
    } else {
      new_kf_pose = Tn_o * old_kf_pose;
    }
    Tn_os[kf_it->second] = Tn_o;
    kf_it->second->set_pose(new_kf_pose);
    //    LOG(INFO)<<"update delta pose: "<<kf_it->first<<" "<<Tn_o.translation().transpose();
    if (kf_it->first < new_it->first) {
      kf_it++;
    } else if (kf_it->first > new_it->first) {
      new_it++;
    } else {
      kf_it++;
      new_it++;
    }
  }

  while (kf_it != kf_end) {
    Tn_os[kf_it->second] = Tn_o;
    auto old_kf_pose = kf_it->second->pose();
    auto new_kf_pose = Tn_o * old_kf_pose;
    kf_it->second->set_pose(new_kf_pose);
    kf_it++;
  }

  for (auto& id2mpt : map_points_) {
    if(id2mpt.second->IsBad() || id2mpt.second->observations().empty()) continue;
    auto wpt = id2mpt.second->WorldPoint3d();
    auto anchor_frm = id2mpt.second->observations().begin()->first;
    if (!Tn_os.count(anchor_frm)) continue;
    auto delta_T = Tn_os[anchor_frm];
    Eigen::Vector3d new_wpt = delta_T * wpt;
    id2mpt.second->SetWorldPoint3d(new_wpt);
  }

  for (auto& id2kf : key_frames_) {
    id2kf.second->set_stable_pose(id2kf.second->pose_vec());
  }
  return;
}

void Map::CheckMap(const std::string& tag) {
  // check observation match
  for (auto mpt : map_points_) {
    for (auto kf2obss : mpt.second->observations()) {
      for (auto& ob : kf2obss.second) {
        auto kf_mpt = kf2obss.first->GetMapPoint(ob.cam_id, ob.feat_id);
        if (kf_mpt != mpt.second) {
          LOG(ERROR) << tag << " CheckMap fail mpt match error";
        }
      }
    }
  }

  for (auto& id2kf : key_frames_) {
    auto kf = id2kf.second;
    auto mpts = kf->map_points(kf->master_cam_id());
    for (auto i = 0; i < mpts.size(); i++) {
      if (!mpts[i]) continue;
      auto kf2obss = mpts[i]->observations();
      if (!kf2obss.count(kf)) {
        LOG(ERROR) << tag << " CheckMap fail, kf match error";
      }
      auto it = kf2obss.at(kf).find(MapPoint::Observation(kf->master_cam_id()));
      if (it == kf2obss.at(kf).end()) {
        LOG(ERROR) << tag << " CheckMap fail, kf match error 2";
      } else if (it->feat_id != i) {
        LOG(ERROR) << tag << " CheckMap fail, kf match error 3";
      }
    }
  }

  size_t pp = 0;
  for (auto& id2kf : key_frames_) {
    for (auto& kf : id2kf.second->connected_kf_weights()) {
      auto frm_id = kf.first->frame_id();
      pp += frm_id;
    }
  }

  VLOG(2) << tag << " CheckMap success pp " << pp;
}

void Map::ObservationCountAnalyze(const std::string& tag) {
  pt_obs_cnt_.clear();
  kf_obs_cnt_.clear();

  VLOG(2) << "mpt size is: " << map_points_.size();
  for (auto& id2mpt : map_points_) {
    pt_obs_cnt_[id2mpt.second] = id2mpt.second->NumOfObs();
  }
  for (auto& id2kf : key_frames_) {
    kf_obs_cnt_[id2kf.second] = id2kf.second->map_point_size();
  }

  std::vector<size_t> ob_size(20, 0);
  for (auto& pt2ob : pt_obs_cnt_) {
    auto num = pt2ob.second;
    if (num > 20) num = 19;
    size_t idx = num;
    ob_size[idx]++;
  }

  std::stringstream ss;
  for (int i = 0; i < ob_size.size(); i++) {
    ss << "(" << i << ": " << ob_size[i] << ") ";
  }
  VLOG(2) << "map point ob size, " << tag << " : " << ss.str();

  std::vector<size_t> reproj_errors(30, 0);
  for (auto& id2mpt : map_points_) {
    auto mpt = id2mpt.second;
    if (mpt->IsBad()) {
      continue;
    }
    for (auto kf2obs : mpt->observations()) {
      CHECK_NE(kf2obs.first, nullptr);

      for (auto& ob : kf2obs.second) {
        CHECK_NE(kf2obs.first, nullptr);
        float err = lm::ComputeProjectError(mpt, kf2obs.first, ob.cam_id, ob.feat_id);
        if (err > 30) err = 29;
        reproj_errors[static_cast<int>(err)]++;
      }
    }
  }

  std::stringstream ss2;
  for (int i = 0; i < reproj_errors.size(); i++) {
    ss2 << "(" << i << ": " << reproj_errors[i] << ") ";
  }
  VLOG(2) << "reproj erros " << ss2.str();

  return;
}

void Map::Reset() {
  replacements_.clear();

  entry_id_to_kfs_.clear();
  kf_to_entry_ids_.clear();
  pt_obs_cnt_.clear();
  kf_obs_cnt_.clear();
  for (auto id_kf : key_frames_) {
    delete id_kf.second;
  }
  key_frames_.clear();
  for (auto id_pt : map_points_) {
    delete id_pt.second;
  }
  map_points_.clear();
  delete_key_frames_buffer_.clear();
  delete_map_points_buffer_.clear();
}

void Map::TryClearKeyFrames() {
  std::unique_lock<std::mutex> lock(kf_mutex_, std::try_to_lock);
  if (!lock.owns_lock()) return;
  for (auto it=delete_key_frames_buffer_.begin(); it!=delete_key_frames_buffer_.end();) {
    if ((*it)->localization_anchor_) { ++it; continue; }
    EraseKeyFrame(*it);
    it=delete_key_frames_buffer_.erase(it);
  }
}

void Map::TryClearMapPoints() {
  std::unique_lock<std::mutex> lock(mpt_mutex_, std::try_to_lock);
  if (!lock.owns_lock()) return;
  for (auto& mpt : delete_map_points_buffer_) {
    EraseMapPoint(mpt);
  }
  delete_map_points_buffer_.clear();
}

void Map::SaveTraj(const std::string& path) {
  PoseWriter writer(path);
  for (auto& id2kf : key_frames_) {
    writer.WritePose(id2kf.second->timestamp(), id2kf.second->pose_vec());
  }
}

}  // namespace dm::vio::lm
