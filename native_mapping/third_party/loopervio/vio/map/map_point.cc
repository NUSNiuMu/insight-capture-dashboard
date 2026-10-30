// Copyright 2020 DeepMirror Inc. All rights reserved.
#include "vio/map/map_point.h"
#include <algorithm>
#include <set>
#include <string>
#include <utility>
#include <vector>
#include "vio/map/key_frame.h"
#include "vio/map/orb_matcher.h"

namespace dm::vio::lm {
FeatureId MapPoint::public_self_inc_id_ = 0;

bool MapPointCmp::operator()(const MapPoint* mpt0, const MapPoint* mpt1) const {
  return mpt0->point_id() < mpt1->point_id();
}

bool MapPointOptimizeCmp::operator()(const MapPoint* mpt0, const MapPoint* mpt1) const {
  // 1 未fixed的排前面
  // 2 上一次未优化的排前面
  // 3 观测数多的排前面
  // 4 id小的排前面

  if (mpt0->state().fixed != mpt1->state().fixed) return (mpt0->state().fixed ? false : true);
  if (mpt0->state().optimized_at_last_time != mpt1->state().optimized_at_last_time)
    return (mpt0->state().optimized_at_last_time ? false : true);
  int obs_num_0 = mpt0->NumOfAllObs();
  int obs_num_1 = mpt1->NumOfAllObs();
  if (obs_num_0 != obs_num_1) return (obs_num_0 > obs_num_1 ? true : false);
  return mpt0->point_id() < mpt1->point_id();
}

MapPoint::MapPoint(Eigen::Vector3d pt, KeyFrame* kf) {
  point_id_ = public_self_inc_id_++;
  point3d_ = pt;  // Pw
  if (!kf) return;
  first_keyframe_seen_ = kf->frame_id();
  ref_keyframe_ = kf->frame_id();
  RefreshAhp(kf, kf->master_cam_id());
  state_ = {false, false, 0};
}

void MapPoint::AddObservation(KeyFrame* kf, CameraId cam_id, size_t feat_idx,
                              const std::string& tag) {
  if (observations_.count(kf)) {
    CHECK_EQ(observations_[kf].count(Observation(cam_id)), 0)
        << tag << " Map add ob, " << point_id_ << " " << kf->frame_id() << " " << cam_id << " "
        << feat_idx;
  }
  auto role = kf->GetCameraRole(cam_id);
  observations_[kf].insert(Observation(cam_id, feat_idx, role));
}

void MapPoint::AddObservation(KeyFrame* kf, std::set<Observation> obs) {
  observations_[kf].insert(obs.begin(), obs.end());
}

void MapPoint::EraseObservation(KeyFrame* kf, CameraId cam_id) {
  if (observations_.count(kf) == 0) {
    return;
  }
  if (observations_[kf].count(Observation(cam_id)))
    observations_[kf].erase(Observation(cam_id));
  if (observations_[kf].size() == 0) observations_.erase(kf);
  if (NumOfAllObs() < 2) {
    is_bad_ = true;
  }
  // TODO(yixuan): change the anchor kf && is_bad_ can't not make sure All connections have been
  // disconnected
}

void MapPoint::EraseObservation(KeyFrame* kf) {
  if (observations_.count(kf) == 0) {
    return;
  }
  observations_.erase(kf);
  if (observations_.size() == 0) {
    is_bad_ = true;
    VLOG(2) << "mappoint " << point_id_ << " has zero obs";
  }
}

bool MapPoint::IsInKeyFrame(KeyFrame* kf) { return (observations_.count(kf) != 0); }

float MapPoint::FoundRatio() { return static_cast<float>(num_found_ * 1.0f) / num_visible_; }

uint32_t MapPoint::NumOfObs() { return observations_.size(); }

uint32_t MapPoint::NumOfAllObs() const {
  size_t cnt = 0;
  for (auto& ob : observations_) {
    cnt += ob.second.size();
  }
  return cnt;
}

// TODO(yixuan): reduce reduce time consuming by using incremental calculation
void MapPoint::ComputeDescriptorsAndNormal(bool incremental_calculate) {
  if (observations_.empty()) {
    return;
  }
  std::vector<cv::Mat> descriptors_vector;

  Eigen::Vector3d normal_vector = Eigen::Vector3d::Zero();
  int normal_num = 0;
  Eigen::Vector3d Pw = point3d_;

  for (auto it = observations_.begin(), it_end = observations_.end(); it != it_end; ++it) {
    KeyFrame* kf = it->first;
    std::set<Observation> kf_obs = it->second;
    for (auto obs : kf_obs) {
      descriptors_vector.push_back(kf->descriptor(obs.cam_id).row(obs.feat_id));

      dm::math::Rigid3d Twc = kf->camera_pose(obs.cam_id);
      Eigen::Vector3d normal = Pw - Twc.translation();
      normal_vector += normal / normal.norm();
      ++normal_num;
    }
  }

  // average viewing orientation
  normal_vector_ = normal_vector / normal_num;

  // Compute distances between them
  const size_t N = descriptors_vector.size();

  std::vector<std::vector<int>> distances(N, std::vector<int>(N));
  for (size_t i = 0; i < N; i++) {
    distances[i][i] = 0;
    for (size_t j = i + 1; j < N; j++) {
      int distij = ORBMatcher::DescriptorDistance(descriptors_vector[i], descriptors_vector[j]);
      distances[i][j] = distij;
      distances[j][i] = distij;
    }
  }

  // Take the descriptor with least median distance to the rest
  int best_median = INT_MAX;
  int best_idx = 0;
  for (size_t i = 0; i < N; ++i) {
    std::sort(distances[i].begin(), distances[i].end());
    int median = distances[i][0.5 * (N - 1)];
    if (median < best_median) {
      best_median = median;
      best_idx = i;
    }
  }
  descriptor_ = descriptors_vector[best_idx].clone();
}

void MapPoint::ReplacedByMapPoint(MapPoint* map_point, const std::string& tag) {
  if (map_point->point_id() == point_id_) {
    return;
  }

#ifdef __x86_64__

  if (false) {
    std::vector<cv::Mat> first_mpt_rects;
    for (auto kf_obs : observations_) {
      KeyFrame* kf = kf_obs.first;
      for (auto ob : kf_obs.second) {
        cv::Mat img = kf->frame_meta()->images[ob.cam_id].clone();
        cv::Point2f kp = kf->key_points(ob.cam_id)[ob.feat_id].pt;
        float left_top_x = 0;
        if (kp.x - 200 < 0.0) {
          left_top_x = 0.0;
        } else if (kp.x + 200 >= 752) {
          left_top_x = 351;
        } else {
          left_top_x = kp.x - 200;
        }
        cv::Rect rect(left_top_x, 0, 400, 480);
        cv::cvtColor(img, img, cv::COLOR_GRAY2BGR);
        cv::circle(img, kp, 3, cv::Scalar(255, 0, 0), -1);
        first_mpt_rects.push_back(img(rect));
      }
    }

    std::vector<cv::Mat> second_mpt_rects;
    for (auto kf_obs : map_point->observations()) {
      KeyFrame* kf = kf_obs.first;
      for (auto ob : kf_obs.second) {
        cv::Mat img = kf->frame_meta()->images[ob.cam_id].clone();
        cv::Point2f kp = kf->key_points(ob.cam_id)[ob.feat_id].pt;
        float left_top_x = 0;
        if (kp.x - 200 < 0.0) {
          left_top_x = 0.0;
        } else if (kp.x + 200 >= 752) {
          left_top_x = 351;
        } else {
          left_top_x = kp.x - 200;
        }
        cv::Rect rect(left_top_x, 0, 400, 480);
        cv::cvtColor(img, img, cv::COLOR_GRAY2BGR);
        cv::circle(img, kp, 3, cv::Scalar(0, 0, 255), -1);
        second_mpt_rects.push_back(img(rect));
      }
    }

    cv::Mat black_img = cv::Mat::zeros(480, 400, CV_8UC3);
    int rects_size = std::max(first_mpt_rects.size(), second_mpt_rects.size());
    if (first_mpt_rects.size() < rects_size) {
      first_mpt_rects.insert(first_mpt_rects.end(), rects_size - first_mpt_rects.size(), black_img);
    }
    if (second_mpt_rects.size() < rects_size) {
      second_mpt_rects.insert(second_mpt_rects.end(), rects_size - second_mpt_rects.size(),
                              black_img);
    }
    cv::Mat first_row_img, second_row_img;
    cv::hconcat(first_mpt_rects, first_row_img);
    cv::hconcat(second_mpt_rects, second_row_img);
    cv::Mat out_img;
    std::vector<cv::Mat> imgs;
    imgs.push_back(first_row_img);
    imgs.push_back(second_row_img);
    cv::vconcat(imgs, out_img);
    cv::imwrite("./images/Fuse/" + std::to_string(point_id_) + "_" +
                    std::to_string(map_point->point_id()) + ".png",
                out_img);
  }
#endif

  int n_visible = num_visible_;
  int n_found = num_found_;

  for (auto it = observations_.begin(), it_end = observations_.end(); it != it_end; ++it) {
    KeyFrame* kf = it->first;
    if (!map_point->IsInKeyFrame(kf)) {
      for (auto const& ob : it->second) {
        map_point->AddObservation(kf, ob.cam_id, ob.feat_id, "adob8");
        kf->ReplaceMapPointMatch(map_point, ob.cam_id, ob.feat_id);
      }
    } else {
      for (auto const& ob : it->second) {
        kf->EraseMapPointMatch(ob.cam_id, ob.feat_id);
      }
      --n_visible;
      --n_found;
    }
  }
  map_point->IncreaseVisible(n_visible);
  map_point->IncreaseFound(n_found);
  map_point->ComputeDescriptorsAndNormal();

  observations_.clear();
  replacement_ = map_point->point_id();
  is_bad_ = true;
}

bool MapPoint::CheckAllObsValidateIn(MapPoint* pt) {
  Eigen::Vector3d Pw = pt->WorldPoint3d();
  for (auto kf_obs = observations_.begin(); kf_obs != observations_.end(); ++kf_obs) {
    KeyFrame* kf = kf_obs->first;
    auto obs = kf_obs->second;
    for (auto ob = obs.begin(); ob != obs.end(); ++ob) {
      CameraId cam_id = ob->cam_id;
      dm::math::Rigid3d Twc = kf->camera_pose(cam_id);
      Eigen::Vector3d Pc = Twc.Inverse() * Pw;
      if (Pc[2] < 0.1) return false;

      float e2 =
          (Pc.head(2) / Pc[2] - kf->key_points_undistorted(ob->cam_id)[ob->feat_id]).squaredNorm();

      int kp_level = kf->key_points(ob->cam_id)[ob->feat_id].octave;
      float kp_sigma2 = KeyFrame::orb_extractor_->GetScaleSigmaSquares()[kp_level];

      if (e2 > 5.99 * kp_sigma2 * KeyFrame::InverseF2()) return false;
    }
  }
  return true;
}

float MapPoint::ComputeTriAngle(float percent) {
  std::vector<std::pair<KeyFrame*, Observation>> kf2obs_vec;
  for (auto& kf2obs : observations_) {
    for (auto& ob : kf2obs.second) {
      kf2obs_vec.push_back(std::make_pair(kf2obs.first, ob));
    }
  }
  std::vector<dm::math::Rigid3d> Twcs;
  for (auto& kf2ob : kf2obs_vec) {
    Twcs.emplace_back(kf2ob.first->camera_pose(kf2ob.second.cam_id));
  }

  if (kf2obs_vec.size() < 2) return 0;

  std::vector<double> tri_angles;
  for (size_t i = 0; i < Twcs.size(); i++) {
    Eigen::Vector3d ray0 = point3d_ - Twcs[i].translation();
    ray0.normalize();
    for (size_t j = i; j < Twcs.size(); j++) {
      Eigen::Vector3d ray1 = point3d_ - Twcs[j].translation();
      ray1.normalize();
      double angle = 180 / M_PI * std::acos(ray0.dot(ray1));
      tri_angles.push_back(angle);
    }
  }
  std::sort(tri_angles.begin(), tri_angles.end());
  return tri_angles[tri_angles.size() * percent];
}

bool MapPoint::IsInFrustum(KeyFrame* kf, float viewing_cos_limit, CameraId cam_id,
                           FrustumInfo* frustum_info) {
  // 3D in absolute coordinates
  Eigen::Vector3d Pw = WorldPoint3d();
  // master camera pose
  dm::math::Rigid3d T_wc = kf->camera_pose(cam_id);
  // 3D in camera coordinates
  Eigen::Vector3d Pc = T_wc.Inverse() * Pw;

  if (Pc.z() < 0.1f) {
    return false;
  }
  Eigen::Vector2d xy = Eigen::Vector2d(Pc.x() / Pc.z(), Pc.y() / Pc.z());
  Eigen::Vector2d uv = KeyFrame::Intrinsic(cam_id).ProjectRayToPoint(xy);
  if (!KeyFrame::Intrinsic(cam_id).InBorder(uv)) {
    return false;
  }

  Eigen::Vector3d vector_P_C = Pw - T_wc.translation();
  float dist = vector_P_C.norm();
  // SuperPoint has no ORB octave-derived distance interval.
  // Check viewing angle
  Eigen::Vector3d P_normal = GetNormal();
  double view_cos = vector_P_C.dot(P_normal) / dist;
  if (view_cos < viewing_cos_limit) {
    return false;
  }
  if (frustum_info) {
    frustum_info->depth = Pc[2];
    frustum_info->in_view = true;
    frustum_info->view_cos = view_cos;
    frustum_info->project_uv = uv;
    frustum_info->distance = dist;
  }
  return true;
}

int MapPoint::PredictScale(float, float) {
  // All supplied SuperPoint observations use native-image coordinates.
  return 0;
}

}  // namespace dm::vio::lm
