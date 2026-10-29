// Copyright 2020 DeepMirror Inc. All rights reserved.
#include "vio/map/orb_matcher.h"
#include <algorithm>
#include <string>
#include <unordered_set>
#include <utility>
namespace dm::vio::lm {
cv::Ptr<cv::BFMatcher> ORBMatcher::matcher_ = cv::BFMatcher::create(cv::NORM_L2, true);

float CompueEpipolarDist(const Eigen::Vector2d& x0, const Eigen::Vector2d& x1,
                         const dm::math::Rigid3d& T10) {
  Eigen::Matrix3d E = T10.EssentialMatrix();
  Eigen::Vector3d l = E * Eigen::Vector3d(x0.x(), x0.y(), 1.0);
  float d = Eigen::Vector3d(x1.x(), x1.y(), 1.0).dot(l) / l.head(2).norm();
  return std::fabs(d);
}

float CompueEpipolarDist(const Eigen::Vector2d& x0, const Eigen::Vector2d& x1,
                         const Eigen::Matrix3d& E10) {
  Eigen::Vector3d l = E10 * Eigen::Vector3d(x0.x(), x0.y(), 1.0);
  float d = Eigen::Vector3d(x1.x(), x1.y(), 1.0).dot(l) / l.head(2).norm();
  return std::fabs(d);
}

std::vector<cv::DMatch> ORBMatcher::SearchByProjection(KeyFrame* query_kf, CameraId query_cam,
                                                       KeyFrame* train_kf, CameraId train_cam,
                                                       float search_radius,
                                                       bool bMono) {
  std::vector<int> vMatches12(query_kf->FeatureSize(query_cam), -1);
  std::vector<int> best_dists(query_kf->FeatureSize(query_cam), 256);

  std::vector<int> rotHist[HISTO_LENGTH];
  for (int i = 0; i < HISTO_LENGTH; i++) rotHist[i].reserve(500);

  const float factor = 1.0f / HISTO_LENGTH;

  // judge the camera is forward or goback
  double stereo_baseline = 0.11;
  dm::math::Rigid3d Twc_query = query_kf->camera_pose(query_cam);
  dm::math::Rigid3d Twc_train = train_kf->camera_pose(train_cam);
  dm::math::Rigid3d Tctrain_query = Twc_train.Inverse() * Twc_query;
  bool forward = Tctrain_query.translation()[2] > stereo_baseline;
  bool backward = (-Tctrain_query.translation()[2]) > stereo_baseline;

  std::vector<MapPoint*> map_points = train_kf->map_points(train_cam);

  std::vector<std::pair<MapPoint*, size_t>> sorted_mpts;
  for (size_t i = 0; i < map_points.size(); i++) {
    if (!map_points[i] || map_points[i]->IsBad()) continue;
    size_t num_obs = map_points[i]->NumOfObs();
    sorted_mpts.emplace_back(std::make_pair(map_points[i], i));
  }
  if (sorted_mpts.empty()) return std::vector<cv::DMatch>();
  std::sort(sorted_mpts.begin(), sorted_mpts.end(),
            [](const std::pair<MapPoint*, size_t>& a, const std::pair<MapPoint*, size_t>& b) {
              if (a.first->NumOfObs() > b.first->NumOfObs()) return true;
              if (a.first->NumOfObs() < b.first->NumOfObs()) return false;
              return a.first->point_id() < b.first->point_id();
            });

  for (auto mpt2feat_idx : sorted_mpts) {
    MapPoint* mpt = mpt2feat_idx.first;

    Eigen::Vector3d Pw = mpt->WorldPoint3d();
    dm::math::Rigid3d Twc = query_kf->camera_pose(query_cam);
    Eigen::Vector3d Pc = Twc.Inverse() * Pw;

    if (Pc[2] < 0.0) {
      continue;
    }

    Eigen::Vector2d xy(Pc[0] / Pc[2], Pc[1] / Pc[2]);
    Eigen::Vector2d uv = KeyFrame::Intrinsic(query_cam).ProjectRayToPoint(xy);

    if (!KeyFrame::Intrinsic(query_cam).InBorder(uv)) {
      continue;
    }

    int octave = train_kf->Octave(train_cam, mpt2feat_idx.second);
    float radius = search_radius * train_kf->LevelFactor(train_cam, mpt2feat_idx.second);

    std::vector<size_t> feat_indices;
    if (forward) {
      feat_indices = query_kf->GetFeatureInArea(uv, radius, octave);
    } else if (backward) {
      feat_indices = query_kf->GetFeatureInArea(uv, radius, 0, octave);
    } else {
      feat_indices = query_kf->GetFeatureInArea(uv, radius, octave - 1, octave + 1);
    }

    if (feat_indices.empty()) {
      continue;
    }
    const cv::Mat descriptor = train_kf->descriptor(train_cam).row(mpt2feat_idx.second);
    int best_dist = 256, second_dist = 256;
    int best_idx = -1;
    const auto& kp2 = train_kf->key_points(train_cam)[mpt2feat_idx.second];

    for (size_t j = 0; j < feat_indices.size(); ++j) {
      const size_t idx = feat_indices[j];
      if (query_kf->GetMapPoint(query_cam, idx) || vMatches12[idx] != -1) {
        continue;
      }

      const cv::Mat& d = query_kf->descriptor(query_cam).row(idx);
      const int dist = DescriptorDistance(descriptor, d);
      if (dist < best_dist) {
        second_dist=best_dist; best_dist=dist; best_idx=idx;
      } else if(dist<second_dist) second_dist=dist;
    }

    if (best_dist <= TH_HIGH && best_dist < nnratio_ * second_dist) {
      vMatches12[best_idx] = mpt2feat_idx.second;
      best_dists[best_idx] = best_dist;
      const auto& kp1 = query_kf->key_points(query_cam)[best_idx];
      if (check_orientation_) {
        float rot = kp1.angle - kp2.angle;
        if (rot < 0.0) rot += 360.0f;
        int bin = round(rot * factor);
        if (bin == HISTO_LENGTH) bin = 0;
        assert(bin >= 0 && bin < HISTO_LENGTH);
        rotHist[bin].push_back(best_idx);
      }
    }
  }

  if (check_orientation_) {
    int ind1 = -1;
    int ind2 = -1;
    int ind3 = -1;
    ComputeThreeMaxima(rotHist, HISTO_LENGTH, &ind1, &ind2, &ind3);
    for (int i = 0; i < HISTO_LENGTH; i++) {
      if (i == ind1 || i == ind2 || i == ind3) continue;
      for (size_t j = 0, jend = rotHist[i].size(); j < jend; j++) {
        vMatches12[rotHist[i][j]] = -1;
      }
    }
  }

  std::vector<cv::DMatch> matches;
  for (size_t i = 0, iend = vMatches12.size(); i < iend; i++) {
    if (vMatches12[i] < 0) continue;
    matches.push_back(cv::DMatch(i, vMatches12[i], best_dists[i]));
  }
  return matches;
}

int ORBMatcher::SearchByProjection(KeyFrame* F, const MapPointSet& map_points,
                                   float search_scale, bool far_points_limit,
                                   float threshold_far_points) {
  int n_matchers = 0;
  CameraId master_cam = F->master_cam_id();
  auto mpts_in_F = F->map_points(master_cam);
  std::unordered_set<MapPoint*> mpts_in_F_set(mpts_in_F.begin(), mpts_in_F.end());

  for (auto& map_point : map_points) {
    if (mpts_in_F_set.count(map_point)) {
      continue;
    }
    MapPoint::FrustumInfo frustum_info = map_point->frustum_infos().at(F);
    // if (far_points_limit && frustum_info.depth > threshold_far_points) {
    //   continue;
    // }
    // The size of the window will depend on the viewing direction, search keypoints in master
    // camera
    float r = RadiusByViewingCos(frustum_info.view_cos) * search_scale;
    float factor = KeyFrame::orb_extractor_->GetScaleFactors()[frustum_info.scale_level];
    const std::vector<size_t> feat_indices =
        F->GetFeatureInArea(frustum_info.project_uv, r * factor, frustum_info.scale_level - 1,
                            frustum_info.scale_level);
    if (feat_indices.empty()) continue;
    const cv::Mat descriptor = map_point->GetDescriptor();

    int best_dist = 256;
    int best_level = -1;
    int best_dist_2 = 256;
    int best_level_2 = -1;
    int best_idx = -1;

    // Get best and second matches with near keypoints
    for (size_t i = 0; i < feat_indices.size(); ++i) {
      const size_t idx = feat_indices[i];
      if (F->GetMapPoint(master_cam, idx)) {
        continue;
      }
      const cv::Mat& d = F->descriptor(master_cam).row(idx);
      const int dist = DescriptorDistance(descriptor, d);
      if (dist < best_dist) {
        best_dist_2 = best_dist;
        best_dist = dist;
        best_level_2 = best_level;
        best_level = F->Octave(master_cam, idx);
        best_idx = idx;
      } else if (dist < best_dist_2) {
        best_level_2 = F->Octave(master_cam, idx);
        best_dist_2 = dist;
      }
    }

    // make sure that the best_dist feat and best_dist_2 feat are sufficiently different
    if (best_dist <= TH_HIGH) {
      if (best_level == best_level_2 && best_dist > best_dist_2 * nnratio_) continue;
      F->AddMapPoint(map_point, master_cam, best_idx, "admp10");
      ++n_matchers;

#ifdef __x86_64__
      if (false) {
        std::vector<cv::Mat> rects;

        cv::Mat cur_img = F->frame_meta()->images[master_cam].clone();
        cv::Point2f cur_kp = F->key_points(master_cam)[best_idx].pt;
        float cur_left_top_x = 0;
        if (cur_kp.x - 200 < 0.0) {
          cur_left_top_x = 0.0;
        } else if (cur_kp.x + 200 >= 752) {
          cur_left_top_x = 351;
        } else {
          cur_left_top_x = cur_kp.x - 200;
        }
        cv::Rect cur_rect(cur_left_top_x, 0, 400, 480);
        cv::cvtColor(cur_img, cur_img, cv::COLOR_GRAY2BGR);

        cv::circle(cur_img, cur_kp, 3, cv::Scalar(255, 0, 0), -1);

        rects.push_back(cur_img(cur_rect));

        for (auto kf_obs : map_point->observations()) {
          KeyFrame* kf = kf_obs.first;
          for (auto obs : kf_obs.second) {
            // rect 每个观测的一部分，其中当前帧，找到的地图点对应的特征点用特殊颜色标识
            cv::Point2f kp = kf->key_points(obs.cam_id)[obs.feat_id].pt;
            float left_top_x = 0;
            if (kp.x - 200 < 0.0) {
              left_top_x = 0.0;
            } else if (kp.x + 200 >= 752) {
              left_top_x = 351;
            } else {
              left_top_x = kp.x - 200;
            }
            cv::Rect rect(left_top_x, 0, 400, 480);
            cv::Mat img = kf->frame_meta()->images[obs.cam_id].clone();
            cv::cvtColor(img, img, cv::COLOR_GRAY2BGR);

            cv::circle(img, kp, 3, cv::Scalar(0, 0, 255), -1);
            rects.push_back(img(rect));
          }
        }

        if (rects.size() > 0) {
          cv::Mat out_img;
          cv::hconcat(rects, out_img);
          cv::imwrite("./images/localproject/" + std::to_string(F->frame_id()) + "_" +
                          std::to_string(map_point->point_id()) + ".png",
                      out_img);
        }
      }
#endif
    }
  }
  return n_matchers;
}

float ORBMatcher::RadiusByViewingCos(float view_cos) {
  if (view_cos > 0.998)
    return 2.5;
  else
    return 4.0;
}

// Bit set count operation from
// http://graphics.stanford.edu/~seander/bithacks.html#CountBitsSetParallel
int ORBMatcher::DescriptorDistance(const cv::Mat& a, const cv::Mat& b) {
  return static_cast<int>(std::lround(200.0 * cv::norm(a, b, cv::NORM_L2)));
}

// 1 find the matches using voc
// 2 filter by descriptor distance
// 3 remove outliers by epipolarConstrain
// 4 remove outlier by keypoint angle,remove the matches that angle diffs are not
// consistent with others
std::vector<cv::DMatch> ORBMatcher::MatchByVoc(KeyFrame* query_kf, CameraId query_cam,
                                               KeyFrame* train_kf, CameraId train_cam,
                                               bool check_orb_level, bool skip_mpts, bool strict) {
  const DBoW3::FeatureVector& vFeatVec1 = query_kf->feat_vecs(query_cam);
  const DBoW3::FeatureVector& vFeatVec2 = train_kf->feat_vecs(train_cam);

  std::vector<bool> vbMatched2(train_kf->FeatureSize(train_cam), false);
  std::vector<int> vMatches12(query_kf->FeatureSize(query_cam), -1);
  std::vector<int> best_dists(query_kf->FeatureSize(query_cam), 256);
  std::vector<int> rotHist[HISTO_LENGTH];
  for (int i = 0; i < HISTO_LENGTH; i++) rotHist[i].reserve(500);

  const float factor = 1.0f / HISTO_LENGTH;

  dm::math::Rigid3d Twq = query_kf->camera_pose(query_cam);
  dm::math::Rigid3d Twt = train_kf->camera_pose(train_cam);
  dm::math::Rigid3d Ttq = Twt.Inverse() * Twq;

  Eigen::Matrix3d E21 = Ttq.EssentialMatrix();
  int orb_th = strict ? TH_LOW : (TH_LOW + TH_HIGH) / 2;

  // Compute epipole in train kf
  Eigen::Vector3d P_train_query = Twt.Inverse() * Twq.translation();
  float ex = P_train_query[0] / P_train_query[2];
  float ey = P_train_query[1] / P_train_query[2];

  DBoW3::FeatureVector::const_iterator f1it = vFeatVec1.begin();
  DBoW3::FeatureVector::const_iterator f2it = vFeatVec2.begin();
  DBoW3::FeatureVector::const_iterator f1end = vFeatVec1.end();
  DBoW3::FeatureVector::const_iterator f2end = vFeatVec2.end();

  while (f1it != f1end && f2it != f2end) {
    if (f1it->first == f2it->first) {
      for (size_t i1 = 0, iend1 = f1it->second.size(); i1 < iend1; i1++) {
        const size_t idx1 = f1it->second[i1];
        if (skip_mpts && query_kf->GetMapPoint(query_cam, idx1)) continue;
        const cv::Mat& d1 = query_kf->descriptor(query_cam).row(idx1);
        const auto& kp1 = query_kf->key_points(query_cam)[idx1];
        const auto& unkp1 = query_kf->key_points_undistorted(query_cam)[idx1];
        int level_1 = kp1.octave;

        int bestDist = orb_th;
        int bestIdx2 = -1;

        for (size_t i2 = 0, iend2 = f2it->second.size(); i2 < iend2; i2++) {
          size_t idx2 = f2it->second[i2];
          if (skip_mpts && train_kf->GetMapPoint(train_cam, idx2)) continue;

          // If we have already matched or there is a MapPoint skip
          if (vbMatched2[idx2]) continue;
          const cv::Mat& d2 = train_kf->descriptor(train_cam).row(idx2);
          const auto& unkp2 = train_kf->key_points_undistorted(train_cam)[idx2];
          int level_2 = train_kf->key_points(train_cam)[idx2].octave;

          if (check_orb_level && (level_2 < level_1 - 1 || level_2 > level_1)) continue;

          const int dist = DescriptorDistance(d1, d2);
          if (dist > orb_th || dist >= bestDist) {
            continue;
          }

          auto inverse_f2 = KeyFrame::InverseF2();
          if (strict) {
            float dist_ex = ex - unkp2.x();
            float dist_ey = ey - unkp2.y();
            if (dist_ex * dist_ex + dist_ey * dist_ey <
                100 * inverse_f2 * KeyFrame::orb_extractor_->GetScaleFactors()[level_2]) {
              continue;
            }
          }

          float epipolor_dist = CompueEpipolarDist(unkp1, unkp2, E21);
          if (epipolor_dist * epipolor_dist >
              3.84 * KeyFrame::orb_extractor_->GetScaleSigmaSquares()[level_2] * inverse_f2) {
            continue;
          }

          bestIdx2 = idx2;
          bestDist = dist;
        }

        if (bestIdx2 >= 0) {
          vMatches12[idx1] = bestIdx2;
          best_dists[idx1] = bestDist;
          vbMatched2[bestIdx2] = true;
          const auto& kp2 = train_kf->key_points(train_cam)[bestIdx2];
          if (check_orientation_) {
            float rot = kp1.angle - kp2.angle;
            if (rot < 0.0) rot += 360.0f;
            int bin = round(rot * factor);
            if (bin == HISTO_LENGTH) bin = 0;
            assert(bin >= 0 && bin < HISTO_LENGTH);
            rotHist[bin].push_back(idx1);
          }
        }
      }
      f1it++;
      f2it++;
    } else if (f1it->first < f2it->first) {
      f1it = vFeatVec1.lower_bound(f2it->first);
    } else {
      f2it = vFeatVec2.lower_bound(f1it->first);
    }
  }

  if (check_orientation_) {
    int ind1 = -1;
    int ind2 = -1;
    int ind3 = -1;
    ComputeThreeMaxima(rotHist, HISTO_LENGTH, &ind1, &ind2, &ind3);
    for (int i = 0; i < HISTO_LENGTH; i++) {
      if (i == ind1 || i == ind2 || i == ind3) continue;
      for (size_t j = 0, jend = rotHist[i].size(); j < jend; j++) {
        vMatches12[rotHist[i][j]] = -1;
      }
    }
  }

  std::vector<cv::DMatch> matches;
  for (size_t i = 0, iend = vMatches12.size(); i < iend; i++) {
    if (vMatches12[i] < 0) continue;
    matches.push_back(cv::DMatch(i, vMatches12[i], best_dists[i]));
  }
  VLOG(2) << "matches by voc " << matches.size();
  return matches;
}

void ORBMatcher::SearchForTriangulation(KeyFrame* query_kf, KeyFrame* train_kf,
                                        std::vector<cv::DMatch>* matches) {
  CameraId cam_id = query_kf->master_cam_id();
  auto descriptor_matches = MatchByVoc(query_kf, cam_id, train_kf, cam_id, false, true, true);
  *matches = std::move(descriptor_matches);
#ifdef __x86_64__
  if (false) {
    cv::Mat out_img;
    cv::drawMatches(query_kf->frame_meta()->images[cam_id], query_kf->key_points(cam_id),
                    train_kf->frame_meta()->images[cam_id], train_kf->key_points(cam_id), *matches,
                    out_img);
    cv::imwrite("./images/neibor/" + std::to_string(query_kf->frame_id()) + "_" +
                    std::to_string(train_kf->frame_id()) + "_" + std::to_string((*matches).size()) +
                    ".png",
                out_img);
  }
#endif
  return;
}

std::vector<cv::DMatch> ORBMatcher::SearchByBoWForLoop(KeyFrame* query_kf, KeyFrame* loop_kf,
                                                       std::vector<MapPoint*>* vpMatches) {
  auto loop_cam = loop_kf->master_cam_id();
  auto query_cam = query_kf->master_cam_id();
  const DBoW3::FeatureVector& vFeatVec1 = loop_kf->feat_vecs(loop_cam);
  const DBoW3::FeatureVector& vFeatVec2 = query_kf->feat_vecs(query_cam);

  *vpMatches = std::vector<MapPoint*>(query_kf->FeatureSize(query_cam), nullptr);
  std::vector<cv::DMatch> cv_matches;

  std::vector<bool> vbMatched2(query_kf->FeatureSize(query_cam), false);
  std::vector<int> vMatches12(loop_kf->FeatureSize(loop_cam), -1);
  std::vector<int> best_dists(loop_kf->FeatureSize(loop_cam), 256);
  std::vector<int> rotHist[HISTO_LENGTH];
  for (int i = 0; i < HISTO_LENGTH; i++) rotHist[i].reserve(500);

  const float factor = 1.0f / HISTO_LENGTH;

  DBoW3::FeatureVector::const_iterator f1it = vFeatVec1.begin();
  DBoW3::FeatureVector::const_iterator f2it = vFeatVec2.begin();
  DBoW3::FeatureVector::const_iterator f1end = vFeatVec1.end();
  DBoW3::FeatureVector::const_iterator f2end = vFeatVec2.end();

  while (f1it != f1end && f2it != f2end) {
    if (f1it->first == f2it->first) {
      for (size_t i1 = 0, iend1 = f1it->second.size(); i1 < iend1; i1++) {
        const size_t idx1 = f1it->second[i1];
        auto mpt = loop_kf->GetMapPoint(loop_cam, idx1);
        if (!mpt || mpt->IsBad()) continue;
        int octave1 = loop_kf->Octave(loop_cam, idx1);
        const cv::Mat& d1 = loop_kf->descriptor(loop_cam).row(idx1);
        int bestDist = 256;
        int bestDist2 = 256;
        int bestIdx = -1;

        for (size_t i2 = 0, iend2 = f2it->second.size(); i2 < iend2; i2++) {
          size_t idx2 = f2it->second[i2];
          // If we have already matched or there is a MapPoint skip
          if (vbMatched2[idx2]) continue;
          int octave2 = query_kf->Octave(query_cam, idx2);
          if (abs(octave1 - octave2) > 2) continue;
          const cv::Mat& d2 = query_kf->descriptor(query_cam).row(idx2);
          const int dist = DescriptorDistance(d1, d2);
          if (dist > bestDist2) continue;
          if (dist < bestDist) {
            bestIdx = idx2;
            bestDist = dist;
          } else {
            bestDist2 = dist;
          }
        }
        bool found_match =
            bestDist < TH_LOW || (bestDist < TH_HIGH && bestDist < nnratio_ * bestDist2);
//        bool found_match = bestDist <TH_LOW;
        if (!found_match) continue;

        vMatches12[idx1] = bestIdx;
        best_dists[idx1] = bestDist;
        vbMatched2[bestIdx] = true;
        const auto& kp2 = query_kf->key_points(query_cam)[bestIdx];
        const auto& kp1 = loop_kf->key_points(loop_cam)[idx1];
        if (check_orientation_) {
          float rot = kp1.angle - kp2.angle;
          if (rot < 0.0)
            rot += 360.0f;
          int bin = round(rot * factor);
          if (bin == HISTO_LENGTH) bin = 0;
          assert(bin >= 0 && bin < HISTO_LENGTH);
          rotHist[bin].push_back(idx1);
        }
      }
      f1it++;
      f2it++;
    } else if (f1it->first < f2it->first) {
      f1it = vFeatVec1.lower_bound(f2it->first);
    } else {
      f2it = vFeatVec2.lower_bound(f1it->first);
    }
  }

  if (check_orientation_) {
    int ind1 = -1;
    int ind2 = -1;
    int ind3 = -1;
    ComputeThreeMaxima(rotHist, HISTO_LENGTH, &ind1, &ind2, &ind3);
    for (int i = 0; i < HISTO_LENGTH; i++) {
      if (i == ind1 || i == ind2 || i == ind3) continue;
      for (size_t j = 0, jend = rotHist[i].size(); j < jend; j++) {
        vMatches12[rotHist[i][j]] = -1;
      }
    }
  }

  std::vector<cv::DMatch> matches;
  size_t matched_cnt = 0;
  for (size_t i = 0, iend = vMatches12.size(); i < iend; i++) {
    if (vMatches12[i] < 0) continue;
    (*vpMatches)[vMatches12[i]] = loop_kf->GetMapPoint(loop_cam, i);
    cv_matches.push_back(cv::DMatch(vMatches12[i], i, 0));
    matched_cnt++;
  }
  return cv_matches;
}

std::vector<cv::DMatch> ORBMatcher::SearchByBfForLoop(KeyFrame* query_kf, KeyFrame* loop_kf,
                                                      std::vector<MapPoint*>* vpMatches) {
  auto loop_cam = loop_kf->master_cam_id();
  auto query_cam = query_kf->master_cam_id();

  std::vector<size_t> orig_loop_indices;
  auto loop_mpts = loop_kf->map_points(loop_cam);
  auto loop_descriptor = loop_kf->descriptor(loop_cam);
  cv::Mat filter_loop_descriptor;

  for (size_t i = 0; i < loop_mpts.size(); i++) {
    if (!loop_mpts[i] || loop_mpts[i]->IsBad() ||
        loop_mpts[i]->NumOfObs() < 3 || !loop_mpts[i]->state().refined) continue;
    filter_loop_descriptor.push_back(loop_descriptor.row(i));
    orig_loop_indices.push_back(i);
  }
  cv::BFMatcher matcher(cv::NORM_L2);
  std::vector<std::vector<cv::DMatch>> matches;
  matcher.knnMatch(filter_loop_descriptor, query_kf->descriptor(query_cam), matches, 2);
  for(auto& pair: matches) for(auto& match: pair) match.distance *= 200.f;

  *vpMatches = std::vector<MapPoint*>(query_kf->FeatureSize(query_cam), nullptr);
  std::vector<cv::DMatch> cv_matches;
  for (auto& match : matches) {
    if (match.size() < 2) continue;
//    LOG(INFO) << " dist is: " << match[1].distance << " " << match[0].distance;
    if (match[0].distance < TH_LOW
        || (match[0].distance < TH_HIGH && match[0].distance < nnratio_ * match[1].distance)) {
      auto loop_idx = orig_loop_indices[match[0].queryIdx];
      auto query_idx = match[0].trainIdx;
      (*vpMatches)[query_idx] = loop_mpts[loop_idx];
      cv_matches.emplace_back(query_idx, loop_idx, match[0].distance);
    }
  }
  return cv_matches;
}

int ORBMatcher::SearchByProjectionForLoop(KeyFrame* query_kf,
                                          const math::Rigid3d& Twi,
                                          const MapPointSet& local_mpts,
                                          std::vector<MapPoint*>* vpMatches,
                                          float radius) {
  auto query_cam = query_kf->master_cam_id();
  if ((*vpMatches).empty()) {
    *vpMatches = std::vector<MapPoint*>(query_kf->FeatureSize(query_cam), nullptr);
  }
  size_t in_img = 0;
  auto camera_pose = Twi * KeyFrame::Extrinsic(query_cam);
  auto Tcw = camera_pose.Inverse();

  cv::Mat query_desc = query_kf->descriptor(query_cam);
  auto kpts = query_kf->key_points(query_cam);
  for (auto& mpt : local_mpts) {
    if (!mpt || mpt->IsBad() || mpt->NumOfObs() < 3 ||
        !mpt->state().refined) continue;
    // project mpt to image
    Eigen::Vector3d world_pt = mpt->WorldPoint3d();
    auto c_pt = Tcw * world_pt;
    if (c_pt[2] < 0) continue;

    auto uv = KeyFrame::Intrinsic(query_cam).ProjectRayToPoint(c_pt.head(2) / c_pt[2]);

    if (KeyFrame::Intrinsic(query_cam).InBorder(uv)) {
      in_img++;
    }

    // get all candiates
    auto indices = query_kf->GetFeatureInArea(uv, radius);
    int best_dist = 256;
    int best_idx = -1;

    int best_dist2 = 256;
    int best_idx2 = -1;

    // if vpMatches have value, continue

    for (auto& idx : indices) {
      if ((*vpMatches)[idx]) continue;
      auto dist = DescriptorDistance(query_desc.row(idx), mpt->GetDescriptor());
      if (dist < best_dist) {
        best_dist2 = best_dist;
        best_idx2 = best_idx;
        best_dist = dist;
        best_idx = idx;
      } else if (dist < best_dist2) {
        best_dist2 = dist;
        best_idx2 = idx;
      }
    }

    if (best_dist < TH_LOW || (best_dist < TH_HIGH && best_dist < nnratio_ * best_dist2)) {
      (*vpMatches)[best_idx] = mpt;
      //      LOG(INFO)<<"err in search is: "<<(uv - Eigen::Vector2d(kpts[best_idx].pt.x,
      //      kpts[best_idx].pt.y)).norm();
    }
  }
  size_t cnt = 0;
  for (auto& mpt : *vpMatches) {
    cnt += (mpt != nullptr);
  }
  VLOG(2) << "in img: " << in_img;
  return cnt;
}

int ORBMatcher::Fuse(KeyFrame* kf, const std::vector<MapPoint*>& map_points, float search_scale,
                     const std::string& tag) {
  int n_fused = 0;
  auto master_cam = kf->master_cam_id();
  for (auto map_point : map_points) {
    if (map_point->IsBad()) {
      continue;
    }

    if (map_point->IsInKeyFrame(kf)) {
      continue;
    }
    MapPoint::FrustumInfo frustum_info;
    if (!map_point->IsInFrustum(kf,
                                0.5,
                                master_cam,
                                &frustum_info))
      continue;
    int predict_level = map_point->PredictScale(frustum_info.distance, map_point->max_distance());

    float r = KeyFrame::orb_extractor_->GetScaleFactors()[predict_level] *
        RadiusByViewingCos(frustum_info.view_cos) * search_scale;
    const std::vector<size_t> feat_indices = kf->GetFeatureInArea(frustum_info.project_uv, r);
    if (feat_indices.empty()) {
      continue;
    }

    const cv::Mat pt3d_descriptor = map_point->GetDescriptor();

    int best_dist = 256;
    int best_idx = -1;
    // Match to the most similar keypoint in the radius
    for (auto feat_index : feat_indices) {
      // check projection error
      cv::KeyPoint kp = kf->key_points(master_cam)[feat_index];
      Eigen::Vector2d uv = Eigen::Vector2d(kp.pt.x, kp.pt.y);
      int kp_level = kp.octave;

      if (kp_level < predict_level - 1 || kp_level > predict_level) continue;

      float kf_sigma2 = KeyFrame::orb_extractor_->GetScaleSigmaSquares()[kp_level];
      if ((uv - frustum_info.project_uv).squaredNorm() > 5.99 * kf_sigma2) {
        continue;
      }
      const cv::Mat& descriptor = kf->descriptor(master_cam).row(feat_index);
      const int dist = DescriptorDistance(pt3d_descriptor, descriptor);
      if (dist < best_dist) {
        best_dist = dist;
        best_idx = feat_index;
      }
    }

    if (best_dist > TH_LOW) continue;

    // If there is already a MapPoint replace otherwise add new measurement
    MapPoint* map_point_in_kf = kf->map_points(master_cam)[best_idx];
    if (map_point_in_kf) {
      if (map_point_in_kf->observations().size() > map_point->observations().size()) {
        if (map_point->CheckAllObsValidateIn(map_point_in_kf)) {
          map_point->ReplacedByMapPoint(map_point_in_kf, tag);
          ++n_fused;
        }
      } else {
        if (map_point_in_kf->CheckAllObsValidateIn(map_point)) {
          map_point_in_kf->ReplacedByMapPoint(map_point, tag);
          ++n_fused;
        }
      }
    } else {
      kf->AddMapPoint(map_point, master_cam, best_idx, true, false, true, "admp8");
      map_point->IncreaseFound();
      map_point->IncreaseVisible();
      map_point->ComputeDescriptorsAndNormal();
      ++n_fused;
    }
  }
  return n_fused;
}

void ORBMatcher::ComputeThreeMaxima(std::vector<int>* histo, int L, int* ind1, int* ind2,
                                    int* ind3) {
  int max1 = 0;
  int max2 = 0;
  int max3 = 0;

  for (int i = 0; i < L; i++) {
    const int s = histo[i].size();
    if (s > max1) {
      max3 = max2;
      max2 = max1;
      max1 = s;
      *ind3 = *ind2;
      *ind2 = *ind1;
      *ind1 = i;
    } else if (s > max2) {
      max3 = max2;
      max2 = s;
      *ind3 = *ind2;
      *ind2 = i;
    } else if (s > max3) {
      max3 = s;
      *ind3 = i;
    }
  }

  if (max2 < 0.1f * static_cast<float>(max1)) {
    *ind2 = -1;
    *ind3 = -1;
  } else if (max3 < 0.1f * static_cast<float>(max1)) {
    *ind3 = -1;
  }
}

}  // namespace dm::vio::lm
