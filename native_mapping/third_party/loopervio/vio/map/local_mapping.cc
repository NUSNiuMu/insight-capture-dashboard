// Copyright 2020 DeepMirror Inc. All rights reserved.
#include "vio/map/local_mapping.h"
#include <glog/logging.h>
#include <algorithm>
#include <map>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

#include "ceres/ceres.h"
#include "common/timer.h"
#include "math/matrix_base.h"
#include "vio/cost_functions/projection_factor.h"
#include "vio/map/ba_optimizer.h"
#include "vio/map/key_frame.h"
#include "vio/map/loop_closure.h"
#include "vio/pose_local_parameterization.h"
#include "vio/triangulation/triangulation.h"

#ifdef __x86_64__
#define LOCAL_MAPPING_DEBUG_MODE 1
#else
#define LOCAL_MAPPING_DEBUG_MODE 0
#endif

namespace dm::vio::lm {

namespace {
constexpr int kMinkeyframeFeatureNum = 40;
constexpr double kKeyFrameCommonFeatureRatio = 0.6;
constexpr double kKeyFrameMinParallaxNormal = 70;
constexpr int kMaxKeyFramesInCurrentLocalMap = 40;
constexpr int kMaxWaitingCandidateFrame = 0;
constexpr int kMaxOptimizeConstraint = 5000;
}  // namespace

LocalMapping::LocalMapping() { map_ = new Map(); }

LocalMapping::LocalMapping(std::shared_ptr<DBoW3::Vocabulary> voc) { map_ = new Map(voc); }

LoopClosure* LocalMapping::loop_closure() { return loop_closure_; }

#ifndef DISABLE_RELOCALIZATION
void LocalMapping::initialize_relocalizer(const RelocalizerWithMapConfig& config) {
  relocalizer_ = std::make_unique<RelocalizerWithMap>(config, map_);
  CHECK_NOTNULL(relocalizer_);
}
#endif

void LocalMapping::set_loop_closure(LoopClosure* loop_closure) { loop_closure_ = loop_closure; }

bool LocalMapping::IsBusy() {
  bool busy = localmapping_buffer_size_ > kMaxWaitingCandidateFrame;
  if (busy) {
    VLOG(1) << "skip lba";
  }
  return busy;
}

bool LocalMapping::IsNewKeyFrame(const FrameMeta::Ptr& candidate_frame) {
  CameraId master_cam = INVALID_CAM_ID;
  for (auto& id2role : candidate_frame->is_master) {
    if (id2role.second) {
      master_cam = id2role.first;
      break;
    }
  }
  if (master_cam == INVALID_CAM_ID) {
    LOG(WARNING) << " there's no master camera in frame "
                 << TimestampFormat(candidate_frame->raw_timestamp);
    return false;
  }

  // if is the first frame
  if (last_added_frm_ == nullptr) return true;

  double delta_t = 1e-9 * (candidate_frame->timestamp - last_added_frm_->timestamp);
  if (delta_t < 0.2) return false;

  // features is few
  if (!candidate_frame->feature_observations.count(master_cam)) return false;
  auto candi_feature_size = candidate_frame->feature_observations.at(master_cam).size();
  if (candi_feature_size < kMinkeyframeFeatureNum) {
    VLOG(2) << "is new keyframe: reason 0 " << candi_feature_size << " "
              << candidate_frame->timestamp;
    return true;
  }

  if (delta_t > 3.0) {
    VLOG(2) << "is new keyframe: reason 1 " << delta_t << " " << candidate_frame->timestamp;
    return true;
  }

  std::vector<float> parallaxes = ComputeParallaxes(last_added_frm_, candidate_frame);
  if (parallaxes.size() < kKeyFrameCommonFeatureRatio * candi_feature_size) {
    VLOG(2) << "is new keyframe: reason 2 " << parallaxes.size() << " " << candi_feature_size
              << " " << candidate_frame->timestamp;
    return true;
  }
  float parallax = parallaxes[0.3 * parallaxes.size()];
  if (parallax > kKeyFrameMinParallaxNormal) {
    VLOG(2) << "is new keyframe: reason 3 " << parallax << " " << candidate_frame->timestamp;
    return true;
  }
  return false;
}

size_t LocalMapping::PnpOptimize() {
  const auto camera=current_key_frame_->master_cam_id();
  const auto& intrinsic=KeyFrame::Intrinsic(camera);
  const auto& points=current_key_frame_->map_points(camera);
  const auto& pixels=current_key_frame_->key_points_undistorted(camera);
  Eigen::Vector6d pose=current_key_frame_->pose_vec();
  std::vector<size_t> supported;
  for(size_t i=0;i<points.size();++i) {
    auto point=points[i];
    if(point && !point->IsBad() && point->NumOfObs()>=3 && point->state().refined)
      supported.push_back(i);
  }
  const auto support=supported.size();
  if(support<40) return 0;
  ceres::Problem problem;
  problem.AddParameterBlock(pose.data(),6,new dm::vio::PoseManifold());
  auto loss=new ceres::HuberLoss(1.0);
  std::vector<ceres::ResidualBlockId> residuals;
  for(auto i:supported) {
    auto point=points[i];
    auto factor=new dm::vio::CameraProjectionFactor(KeyFrame::Extrinsic(camera),
        pixels[i],Eigen::Vector2d(intrinsic.fx(),intrinsic.fy()));
    residuals.push_back(problem.AddResidualBlock(factor,loss,pose.data(),point->point3d_data()));
    problem.SetParameterBlockConstant(point->point3d_data());
  }
  ceres::Solver::Options options;
  options.linear_solver_type=ceres::DENSE_SCHUR;
  options.max_num_iterations=20;
  options.max_solver_time_in_seconds=.1;
  ceres::Solver::Summary summary;
  ceres::Solve(options,&problem,&summary);
  const auto first_camera_from_map=(RigidFromVector(pose)*KeyFrame::Extrinsic(camera)).Inverse();
  size_t retained=0;
  for(size_t j=0;j<supported.size();++j) {
    const auto i=supported[j];
    const auto xyz=first_camera_from_map*points[i]->WorldPoint3d();
    const double error=(xyz.head(2)/xyz[2]-pixels[i]).norm()*intrinsic.f();
    if(xyz[2]<=0 || !std::isfinite(error) || error>3.) problem.RemoveResidualBlock(residuals[j]);
    else ++retained;
  }
  if(retained<40) return 0;
  ceres::Solve(options,&problem,&summary);
  auto camera_from_map=(RigidFromVector(pose)*KeyFrame::Extrinsic(camera)).Inverse();
  std::vector<bool> inlier(points.size(),false);
  size_t good=0; std::set<int> cells;
  for(size_t i=0;i<points.size();++i) {
    auto point=points[i];
    if(!point || point->IsBad()) continue;
    auto xyz=camera_from_map*point->WorldPoint3d();
    double error=(xyz.head(2)/xyz[2]-pixels[i]).norm()*intrinsic.f();
    inlier[i]=xyz[2]>0 && std::isfinite(error) && error<=3.;
    if(!inlier[i] || point->NumOfObs()<3 || !point->state().refined) continue;
    ++good;
    const auto p=current_key_frame_->key_points(camera)[i].pt;
    cells.insert(int(p.y*4/intrinsic.height())*4+int(p.x*4/intrinsic.width()));
  }
  if(good<40 || good<support*.5 || cells.size()<4) return 0;
  current_key_frame_->set_pose(RigidFromVector(pose));
  for(size_t i=0;i<points.size();++i)
    if(points[i] && !inlier[i]) current_key_frame_->EraseMapPointMatch(camera,i);
  return good;
}

void LocalMapping::UpdateParentOfCurrentKeyFrame() {
  if (map_->KeyFrameEmpty()) return;
  auto best_weightedkfs = current_key_frame_->GetBestCovisibilityKeyFramesWithWeight(1);
  if (best_weightedkfs.empty()) {
    auto last_kf = map_->key_frames().rbegin()->second;
    current_key_frame_->ChangeParent(last_kf, 0);
  } else {
    auto parent_kf = best_weightedkfs[0].kf;
    current_key_frame_->ChangeParent(parent_kf, best_weightedkfs[0].weight);
  }
}

void LocalMapping::GetMaxMinDistanceInvariance(MapPoint* pt, float* max_dist, float* min_dist) {
  Eigen::Vector3d Pw = pt->WorldPoint3d();
  KeyFrame* ref_kf = map_->GetKeyFrame(pt->ref_keyframe());
  CameraId master_cam = ref_kf->master_cam_id();
  CameraId cam_id = master_cam;
  int feat_id = -1;
  for (auto obs : pt->observations()[ref_kf]) {
    feat_id = obs.feat_id;
    cam_id = obs.cam_id;
    if (obs.cam_id == master_cam) {
      break;
    }
  }
  CHECK_NE(feat_id, -1);
  dm::math::Rigid3d Twc = ref_kf->camera_pose(cam_id);
  float dist = (Pw - Twc.translation()).norm();
  int level = ref_kf->key_points(cam_id)[feat_id].octave;
  float level_scale_factor = KeyFrame::orb_extractor_->GetScaleFactors()[level];
  int n_levels = KeyFrame::orb_extractor_->GetLevels();

  *max_dist = dist * level_scale_factor;
  *min_dist = (*max_dist) / KeyFrame::orb_extractor_->GetScaleFactors()[n_levels - 1];
}

void LocalMapping::SearchLocalPoints() {
  // Do not search map points already matched
  CameraId master_cam_id = current_key_frame_->master_cam_id();
  ORBMatcher matcher(50, 100, 0.8);
  // Project points in frame and check its visibility
  MapPointSet visible_mpts;
  for (auto map_point : local_map_points_) {
    if (map_point->last_keyframe_seen() == current_key_frame_->frame_id()) {
      continue;
    }
    // project to current keyframe master image
    MapPoint::FrustumInfo frustum_info;
    if (map_point->IsInFrustum(current_key_frame_,
                               0.5,
                               master_cam_id,
                               &frustum_info)) {
      map_point->IncreaseVisible();
      visible_mpts.insert(map_point);
      frustum_info.scale_level =
          map_point->PredictScale(frustum_info.distance, map_point->max_distance());
      map_point->frustum_infos()[current_key_frame_] = frustum_info;
    }
  }
  size_t new_matches_num =
      matcher.SearchByProjection(current_key_frame_, visible_mpts, 3.0, true, 20);
  for(auto point:visible_mpts) point->frustum_infos().erase(current_key_frame_);
  VLOG(2) << "SearchLocalPoints new num_matches is " << new_matches_num << " "
            << visible_mpts.size();
}

size_t LocalMapping::UpdateLocalPoints() {
  // Project the shared map, including landmarks outside the newest covisible frames.
  local_map_points_.clear();
  for(const auto& entry:map_->map_points())
    if(!entry.second->IsBad()) local_map_points_.insert(entry.second);
  return local_map_points_.size();
}

size_t LocalMapping::SearchPointsInLastKeyFrame() {
  KeyFrame* last_keyframe = map_->LastKeyFrame();
  int camera_id = current_key_frame_->master_cam_id();
  ORBMatcher orb_matcher(50, 100, 0.8, true);
  std::vector<cv::DMatch> good_matches;
  auto matches = orb_matcher.SearchByProjection(current_key_frame_, camera_id,
      last_keyframe, camera_id, 10.0f);
  size_t mpt_cnt = 0;
  for (size_t i = 0; i < matches.size(); ++i) {
    int last_kf_feat_idx = matches[i].trainIdx;
    MapPoint* mpt = last_keyframe->GetMapPoint(camera_id, last_kf_feat_idx);
    if (!mpt || mpt->IsBad()) {
      LOG(ERROR) << "there are some bug in SearchPointsInLastKeyFrame";
      continue;
    }
    good_matches.push_back(matches[i]);
    current_key_frame_->AddMapPoint(mpt, camera_id, matches[i].queryIdx);
    mpt->UpateLastKFSeen(current_key_frame_->frame_id());
    mpt->IncreaseVisible();
    mpt_cnt++;
  }
#ifdef __x86_64__
  if (false) {
    cv::Mat out_img;
    cv::drawMatches(current_key_frame_->frame_meta()->images[camera_id],
                    current_key_frame_->key_points(camera_id),
                    last_keyframe->frame_meta()->images[camera_id],
                    last_keyframe->key_points(camera_id), good_matches, out_img);
    cv::imwrite("./images/cur_last/" + std::to_string(current_key_frame_->frame_id()) + "_" +
                    std::to_string(last_keyframe->frame_id()) + "_" +
                    std::to_string(good_matches.size()) + ".png",
                out_img);
  }
#endif
  VLOG(2) << "SearchPointsInLastKeyFrame matches size and mpt size: " << matches.size() << " "
            << mpt_cnt;
  return mpt_cnt;
}

void LocalMapping::UpdateLocalKeyFrames() {
  local_key_frames_.clear();
  // All keyframes that observe a map point are included in the local map.
  std::vector<MapPoint*> map_points =
      current_key_frame_->map_points(current_key_frame_->master_cam_id());
  if (map_points.empty()) {
    KeyFrame* last_keyframe = map_->LastKeyFrame();
    CHECK_NOTNULL(last_keyframe);
    map_points = last_keyframe->map_points(last_keyframe->master_cam_id());
  }
  for (size_t i = 0; i < map_points.size(); ++i) {
    if (!map_points[i] || map_points[i]->IsBad()) continue;
    const auto& observations = map_points[i]->observations();
    for (auto it = observations.begin(); it != observations.end(); ++it) {
      local_key_frames_.insert(it->first);
    }
  }

  KeyFrameSet level_one_keyframes = local_key_frames_;

  // Include also some not-already-included keyframes that are neighbors to already-included
  // keyframes（extending at most three kfs for every already-included keyframes）

  for (auto& kf : level_one_keyframes) {
    if (local_key_frames_.size() > kMaxKeyFramesInCurrentLocalMap) {
      break;
    }
    const std::vector<KeyFrame*> neighbors = kf->GetBestCovisibilityKeyFrames(10);
    for (auto neighbor_kf : neighbors) {
      if (!local_key_frames_.count(neighbor_kf)) {
        local_key_frames_.insert(neighbor_kf);
        break;
      }
    }
    for (auto child : kf->GetChilds()) {
      if (!local_key_frames_.count(child.kf)) {
        local_key_frames_.insert(child.kf);
        break;
      }
    }

    KeyFrame* parent = kf->GetParent().kf;
    if (parent) local_key_frames_.insert(parent);
  }
}

void LocalMapping::AddKeyFrameToMap() {
  if (map_->KeyFrameEmpty()) {
    map_->AddKeyFrame(current_key_frame_);
    return;
  }

  // find current map points in current frame in order to update keyframes connections for current
  // keyframe
  size_t mpt_in_last_kf_size = SearchPointsInLastKeyFrame();
  UpdateLocalKeyFrames();
  size_t local_mpt_size = UpdateLocalPoints();
  SearchLocalPoints();

  CameraId master_cam_id = current_key_frame_->master_cam_id();
  PnpOptimize();
  int pt3d_in_current_kf = 0, total_obs_num = 0;
  // Associate MapPoints to the new keyframe and update descriptor
  std::vector<MapPoint*> map_points = current_key_frame_->map_points(master_cam_id);
  current_key_frame_->map_points(master_cam_id) =
      std::vector<MapPoint*>(map_points.size(), nullptr);
  for (size_t i = 0; i < map_points.size(); i++) {
    auto mpt = map_points[i];
    if (!mpt || mpt->IsBad()) continue;
    current_key_frame_->AddMapPoint(mpt, master_cam_id, i, true, false, true, "admp7");
    mpt->ComputeDescriptorsAndNormal();
    mpt->IncreaseFound();
    pt3d_in_current_kf++;
    total_obs_num += mpt->NumOfObs();
  }

  VLOG(2) << "pt3d in current kf: " << pt3d_in_current_kf << " "
            << float(total_obs_num) / pt3d_in_current_kf;

#ifdef __x86_64__
  cv::Mat img = DrawMapPointsInKeyFrame(current_key_frame_, 0);
  //cv::imshow("img", img);
#endif

  current_key_frame_->UpdateConnections();
  UpdateParentOfCurrentKeyFrame();

  map_->AddKeyFrame(current_key_frame_);
  local_key_frames_.clear();
  local_map_points_.clear();
}

void LocalMapping::MapPointCulling() {
  auto lit = recent_add_map_points_.begin();
  int threshold_obs = 5;
  while (lit != recent_add_map_points_.end()) {
    auto map_point = map_->GetMapPoint(*lit);
    if (!map_point) {
      lit = recent_add_map_points_.erase(lit);
      continue;
    }
    if (map_point->IsBad()) {
      lit = recent_add_map_points_.erase(lit);
      map_->EraseMapPointTS(map_point);
    } else if (map_point->FoundRatio() < 0.3f) {
      lit = recent_add_map_points_.erase(lit);
      map_->EraseMapPointTS(map_point);
    } else if (static_cast<int>(current_key_frame_->frame_id()) -
                   static_cast<int>(map_point->first_keyframe_seen()) >=
               threshold_obs) {
      lit = recent_add_map_points_.erase(lit);
    } else {
      ++lit;
    }
  }
}

size_t LocalMapping::CreateNewMapPointByStereo() {
  CameraId current_master_id = current_key_frame_->master_cam_id();
  CameraId current_slave_id = current_key_frame_->GetSlaveCamera(current_master_id);
  dm::math::Rigid3d current_master_camera_pose = current_key_frame_->camera_pose(current_master_id);
  dm::math::Rigid3d current_slave_camera_pose = current_key_frame_->camera_pose(current_slave_id);
  const auto& s2m_match = current_key_frame_->Slave2MasterMatches(current_slave_id);
  size_t stereo_tri = 0;
  const auto& kpts_grid = current_key_frame_->grid();
  const auto& kpts = current_key_frame_->key_points(current_master_id);

  Triangulation triangulation;
  std::vector<dm::math::Rigid3d> Twcs{current_master_camera_pose, current_slave_camera_pose};

  for (auto r = 0; r < kpts_grid.rows(); r++) {
    for (auto c = 0; c < kpts_grid.cols(); c++) {
      auto feat_indices = kpts_grid.at(r, c);
      for (auto& feat_idx : feat_indices) {
        if (current_key_frame_->GetMapPoint(current_master_id, feat_idx)) continue;
        auto slave_idx = s2m_match.m2s_matches[feat_idx];
        if (slave_idx == -1 || current_key_frame_->GetMapPoint(current_slave_id, slave_idx))
          continue;
        Eigen::Vector2d master_point =
            current_key_frame_->key_points_undistorted(current_master_id)[feat_idx];
        Eigen::Vector2d slave_point =
            current_key_frame_->key_points_undistorted(current_slave_id)[slave_idx];
        std::vector<Eigen::Vector2d> points_2d{master_point, slave_point};

        int master_level = current_key_frame_->Octave(current_master_id, feat_idx);
        int slave_level = current_key_frame_->Octave(current_slave_id, slave_idx);
        float master_sigma2 = KeyFrame::orb_extractor_->GetScaleSigmaSquares()[master_level];
        float slave_sigma2 = KeyFrame::orb_extractor_->GetScaleSigmaSquares()[slave_level];
        float inverse_f2 = KeyFrame::InverseF2();
        std::vector<float> chi2_th{5.99f * master_sigma2 * inverse_f2,
                                   5.99f * slave_sigma2 * inverse_f2};
        auto pt3d = triangulation.triangulateDlt(points_2d, Twcs, true, chi2_th);

        if (!pt3d.has_value()) {
          current_key_frame_->EraseStereoAssociation(current_master_id, current_slave_id, feat_idx);
          continue;
        }

        MapPoint* map_point = new MapPoint(pt3d.value(), current_key_frame_);
        map_point->AddObservation(current_key_frame_, current_master_id, feat_idx, "adob0");
        map_point->AddObservation(current_key_frame_, current_slave_id, slave_idx, "adob1");
        map_point->IncreaseVisible();
        map_point->IncreaseFound();
        current_key_frame_->AddMapPoint(map_point, current_master_id, feat_idx, "admp0");
        current_key_frame_->AddMapPoint(map_point, current_slave_id, slave_idx, "admp1");
        map_point->ComputeDescriptorsAndNormal();
        map_->AddMapPoint(map_point);

        float max_dist, min_dist;
        GetMaxMinDistanceInvariance(map_point, &max_dist, &min_dist);
        map_point->SetMaxDistance(max_dist);

        map_point->SetMinDistance(min_dist);

        recent_add_map_points_.insert(map_point->point_id());
        current_kf_create_map_points_.push_back(map_point);
        stereo_tri++;
      }
    }
  }
  return stereo_tri;
}

size_t LocalMapping::CreateNewMapPointByNeibour() {
  // triangulate points according to best covisibility keyframes
  CameraId current_master_id = current_key_frame_->master_cam_id();
  dm::math::Rigid3d current_master_camera_pose = current_key_frame_->camera_pose(current_master_id);

  const auto& kpts_grid = current_key_frame_->grid();
  Eigen::MatrixXi tri_counts = Eigen::MatrixXi::Zero(kpts_grid.rows(), kpts_grid.cols());
  const auto& kpts = current_key_frame_->key_points(current_master_id);
  for (size_t i = 0; i < kpts.size(); i++) {
    auto mpt = current_key_frame_->GetMapPoint(current_master_id, i);
    if (!mpt || mpt->IsBad()) continue;
    size_t r = kpts_grid.RowIndex(kpts[i].pt.y);
    size_t c = kpts_grid.ColIndex(kpts[i].pt.x);
    tri_counts(r, c)++;
  }

  const double kMinBaseLine = 0.1;
  uint32_t nn = 5;
  std::vector<KeyFrame*> neighbor_kfs = current_key_frame_->GetBestCovisibilityKeyFrames(nn);
  auto all_kfs = map_->key_frames();

  if (neighbor_kfs.empty()) {
    for (auto it = all_kfs.rbegin(); it != all_kfs.rend(); it++) {
      if (neighbor_kfs.size() <= 5 && it->second != current_key_frame_) {
        neighbor_kfs.push_back(it->second);
      }
    }
  }

  size_t tri_cnt = 0;
  ORBMatcher orb_matcher(60, 100, 0.8, true);
  float ratio_factor = 1.5f * KeyFrame::orb_extractor_->GetScaleFactor();

  Triangulation triangulation;
  for (auto kf : neighbor_kfs) {
    dm::math::Rigid3d master_camera_pose = kf->camera_pose(current_master_id);
    double baseline =
        (current_master_camera_pose.translation() - master_camera_pose.translation()).norm();
    VLOG(2) << "CreateNewMapPointByNeibour base line is: " << baseline;
    if (baseline < kMinBaseLine) continue;

    std::vector<cv::DMatch> matches;
    orb_matcher.SearchForTriangulation(current_key_frame_, kf, &matches);

    std::unordered_map<size_t, size_t> matches_map;
    std::vector<cv::DMatch> good_matches;
    for (auto& m : matches) {
      matches_map[m.queryIdx] = m.trainIdx;
    }
    for (auto r = 0; r < kpts_grid.rows(); r++) {
      for (auto c = 0; c < kpts_grid.cols(); c++) {
        if (tri_counts(r, c)) continue;
        auto feat_indices = kpts_grid.at(r, c);
        for (auto& feat_idx : feat_indices) {
          if (!matches_map.count(feat_idx)) continue;

          Eigen::Vector2d current_keyframe_pt =
              current_key_frame_->key_points_undistorted(current_master_id)[feat_idx];
          Eigen::Vector2d kf_pt =
              kf->key_points_undistorted(current_master_id)[matches_map[feat_idx]];
          std::vector<Eigen::Vector2d> points_2d{current_keyframe_pt, kf_pt};
          std::vector<dm::math::Rigid3d> Twcs{current_master_camera_pose, master_camera_pose};

          int current_level = current_key_frame_->Octave(current_master_id, feat_idx);
          int kf_level = kf->Octave(current_master_id, matches_map[feat_idx]);
          float current_sigma2 = KeyFrame::orb_extractor_->GetScaleSigmaSquares()[current_level];
          float kf_sigma2 = KeyFrame::orb_extractor_->GetScaleSigmaSquares()[kf_level];
          float inverse_f2 = KeyFrame::InverseF2();
          std::vector<float> chi2_th{5.99f * current_sigma2 * inverse_f2,
                                     5.99f * kf_sigma2 * inverse_f2};
          auto pt3d = triangulation.triangulateDlt(points_2d, Twcs, true, chi2_th);

          if (!pt3d.has_value()) {
            continue;
          }

          // check scale consistency
          float current_dist = (pt3d.value() - current_master_camera_pose.translation()).norm();
          float kp_dist = (pt3d.value() - master_camera_pose.translation()).norm();
          float ratio_dist = kp_dist / current_dist;
          float ratio_octave = KeyFrame::orb_extractor_->GetScaleFactors()[current_level] /
                               KeyFrame::orb_extractor_->GetScaleFactors()[kf_level];

          if (ratio_dist * ratio_factor < ratio_octave || ratio_dist > ratio_octave * ratio_factor)
            continue;

          MapPoint* map_point = new MapPoint(pt3d.value(), current_key_frame_);
          // cause two kfs observe this map point
          map_point->IncreaseFound(2);
          map_point->IncreaseVisible(2);

          current_key_frame_->AddMapPoint(map_point, current_master_id, feat_idx, true, false, true,
                                          "admp2");
          kf->AddMapPoint(map_point, current_master_id, matches_map[feat_idx], true, false, true,
                          "admp3");
          map_point->ComputeDescriptorsAndNormal();
          map_->AddMapPoint(map_point);

          float max_dist, min_dist;
          GetMaxMinDistanceInvariance(map_point, &max_dist, &min_dist);
          map_point->SetMaxDistance(max_dist);
          map_point->SetMinDistance(min_dist);

          recent_add_map_points_.insert(map_point->point_id());
          current_kf_create_map_points_.push_back(map_point);
          tri_cnt++;
          tri_counts(r, c)++;
          good_matches.push_back(cv::DMatch(feat_idx, matches_map[feat_idx], 100));
        }
      }
    }
#ifdef __x86_64__
    if (false) {
      cv::Mat out_img;
      cv::drawMatches(current_key_frame_->frame_meta()->images[current_master_id],
                      current_key_frame_->key_points(current_master_id),
                      kf->frame_meta()->images[current_master_id],
                      kf->key_points(current_master_id), good_matches, out_img);
      cv::imwrite("./images/neibor_good_matches/" + std::to_string(current_key_frame_->frame_id()) +
                      "_" + std::to_string(kf->frame_id()) + "_" +
                      std::to_string(good_matches.size()) + ".png",
                  out_img);
    }
#endif
  }
  return tri_cnt;
}

size_t LocalMapping::CreateNewMapPoint() {
  if (map_->KeyFrameEmpty()) return 0;
  current_kf_create_map_points_.clear();
  size_t stereo_tri = 0, neibour_tri = 0;
  stereo_tri = CreateNewMapPointByStereo();
  neibour_tri = CreateNewMapPointByNeibour();
  VLOG(2) << "CreateNewMapPoint stereo and neibour num: " << stereo_tri << " " << neibour_tri;
  return stereo_tri + neibour_tri;
}

// map point fusion, is in order to refine the local map
// 1 Retrieve First-level and second-level neighbor frames(named target KFs which is larger range
// than local map); 2 Search matches by project map points which current KF create in target
// KFs(cause other map points in current kf have been fused in last step); 3 Search matches by
// project map points from target KFs in current KF(this step can find some stable map points adding
// to current localmap && fixed kfs in lba)
size_t LocalMapping::FuseWithNeighbors() {
  // first neighbors
  const std::vector<KeyFrame*>& first_neighbors =
      current_key_frame_->GetBestCovisibilityKeyFrames(10);
  KeyFrameSet target_kfs(first_neighbors.begin(), first_neighbors.end());
  size_t fuse_cnt = 0;
  // extend to second neighbors
  KeyFrameSet second_neighbors;
  for (auto kf : target_kfs) {
    const std::vector<KeyFrame*> second_neighbor = kf->GetBestCovisibilityKeyFrames(20);
    second_neighbors.insert(second_neighbor.begin(), second_neighbor.end());
  }
  // remove current frame from second neibors
  auto it = second_neighbors.find(current_key_frame_);
  if (it != second_neighbors.end()) second_neighbors.erase(it);

  target_kfs.insert(second_neighbors.begin(), second_neighbors.end());
  // search matches from current KF to target
  for (auto kf : target_kfs) {
    fuse_cnt += orb_matcher_.Fuse(kf, current_kf_create_map_points_, 3.0, "fuse current");
  }

  // Search matches from target KFs to current KF
  std::vector<MapPoint*> fuse_candidates;
  for (auto kf : target_kfs) {
    for (auto mpt : kf->map_points(kf->master_cam_id())) {
      if (!mpt || mpt->IsBad()) continue;
      fuse_candidates.push_back(mpt);
    }
  }
  fuse_cnt += orb_matcher_.Fuse(current_key_frame_, fuse_candidates, 3.0, "fuse candiates");
  current_key_frame_->UpdateConnections();
  VLOG(2) << "fuse cnt in FuseWithNeighbors " << fuse_cnt;
  return fuse_cnt;
}

void LocalMapping::MapPointFiltering(const std::unordered_set<MapPoint*>& map_points,
                                     std::unordered_set<KeyFrame*> filtered_fixed_kfs) {
  // remove outlier observations
  size_t erase_obs_count = 0, erase_mpt_size = 0, all_obs_size = 0;
  for (auto map_point : map_points) {
    bool recompute_descriptor = false;
    auto kfs_obs = map_point->observations();
    MapPoint::KeyFrame2Observations outlier;
    int obs_num = 0;
    float obs_proj_error = 0.0f;
    for (auto kf_obs = kfs_obs.begin(); kf_obs != kfs_obs.end(); ++kf_obs) {
      KeyFrame* kf = kf_obs->first;
      auto obs = kf_obs->second;
      size_t erase_cnt = 0;
      float scale = 1.0f;
      if (filtered_fixed_kfs.count(kf)) {
        if (map_point->state().fixed) continue;
        scale = 9.0f;
      }
      for (auto ob : obs) {
        double reproject_error =
            lm::ComputeProjectError(map_point, kf, ob.cam_id, ob.feat_id, -1.0);
        int level = kf->key_points(ob.cam_id)[ob.feat_id].octave;
        float sigma2 = KeyFrame::orb_extractor_->GetScaleSigmaSquares()[level];

        if (reproject_error < 0) {
          LOG(WARNING) << "and invalid mpt after local BA";
          map_point->SetBad();
          break;
        } else if (reproject_error * reproject_error > 5.99 * sigma2 * scale) {
          recompute_descriptor = true;
          kf->EraseMapPointMatchSplit(ob.cam_id, ob.feat_id);
          map_point->EraseObservation(kf, ob.cam_id);
          erase_cnt++;
          erase_obs_count++;
          outlier[kf].insert(ob);
        } else {
          ++obs_num;
          obs_proj_error += reproject_error;
        }
      }

      if (map_point->IsBad()) {
        break;
      }

      all_obs_size += obs.size();
      if (erase_cnt == obs.size()) {
        map_point->DecreaseFound();
      }
    }
#ifdef __x86_64__
    if (false) {
      if (map_point->IsBad()) {
        std::vector<cv::Mat> rects;
        for (auto kf_obs : kfs_obs) {
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
            rects.push_back(img(rect));
          }
        }
        cv::Mat out_img;
        cv::hconcat(rects, out_img);
        cv::imwrite("./images/bad_mpt/" + std::to_string(current_key_frame_->frame_id()) + "_" +
                        std::to_string(map_point->point_id()) + ".png",
                    out_img);
      } else {
        std::vector<cv::Mat> rects;
        for (auto kf_obs : kfs_obs) {
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
            if (outlier.count(kf) != 0 && outlier[kf].count(ob) != 0) {
              cv::circle(img, kp, 3, cv::Scalar(0, 0, 255), -1);
            } else {
              cv::circle(img, kp, 3, cv::Scalar(255, 255, 0), -1);
            }
            if (filtered_fixed_kfs.count(kf)) {
              cv::putText(img, "fixed", cv::Point2i(kp.x + 5, kp.y - 5), cv::FONT_HERSHEY_SIMPLEX,
                          0.35, cv::Scalar(0, 0, 255), 1.2, 8);
            }

            rects.push_back(img(rect));
          }
        }
        cv::Mat out_img;
        cv::hconcat(rects, out_img);
        cv::imwrite("./images/outlier/" + std::to_string(current_key_frame_->frame_id()) + "_" +
                        std::to_string(map_point->point_id()) + ".png",
                    out_img);
      }
    }
#endif

    if (map_point->FoundRatio() < 0.3 || map_point->IsBad() || map_point->NumOfAllObs() < 2) {
      erase_mpt_size++;
      map_->EraseMapPointTS(map_point);
      continue;
    } else if (recompute_descriptor) {
      map_point->ComputeDescriptorsAndNormal();
    }

    if (obs_num > 0) map_point->state().refined = obs_proj_error / obs_num < 1.0f;
    if (obs_proj_error / obs_num < 1.0f) {
      map_point->state().optimize_num++;
    } else {
      map_point->state().optimize_num = 0;
    }

    if (map_point->state().optimize_num > 10) {
      map_point->state().fixed = true;
    }
  }
  VLOG(2) << "MapPointFiltering " << map_points.size() << " " << erase_mpt_size << " "
            << erase_obs_count << " " << all_obs_size;
}

int LocalMapping::AddPtToProblem(KeyFrameSet* kfs_need_optimize,
                                 MapPointOptimizeSet* mappoints_need_optimize,
                                 KeyFrameSet* filtered_fixed_kfs,
                                 std::unordered_set<KeyFrame*>* kfs_actual_in_optimize,
                                 std::unordered_set<KeyFrame*>* fixed_kfs_actual_in_optimize,
                                 std::unordered_set<MapPoint*>* mpts_actual_in_optimize,
                                 BAOptimizer* optimizer) {
  int num_fixed_pt = 0;
  size_t variable_constraint = 0, constant_constraint = 0;
  ceres::LossFunction* cauchy_loss = new ceres::CauchyLoss(3.0);
  // add pts in current_key_frame_ firstly, make sure current_key_frame_ is in problem
  for (auto map_point : *mpts_actual_in_optimize) {
    for (auto kf2obs : map_point->observations()) {
      KeyFrame* kf = kf2obs.first;
      if (filtered_fixed_kfs->count(kf)) {
        if (map_point->state().fixed) continue;
        if (!fixed_kfs_actual_in_optimize->count(kf)) {
          fixed_kfs_actual_in_optimize->insert(kf);
        }
      } else if (kfs_need_optimize->count(kf) && !kfs_actual_in_optimize->count(kf)) {
        kfs_actual_in_optimize->insert(kf);
      }

      for (auto ob : kf2obs.second) {
        float w = kf->LevelFactor(ob.cam_id, ob.feat_id);
        if (kfs_need_optimize->count(kf)) {
          variable_constraint++;
        } else {
          constant_constraint++;
          w *= 3;
        }

        Eigen::Vector2d projection_weight = {KeyFrame::Intrinsic(ob.cam_id).fx() / w,
                                             KeyFrame::Intrinsic(ob.cam_id).fy() / w};
        optimizer->AddProjection(map_point, kf, ob.cam_id, ob.feat_id,
                                {projection_weight, cauchy_loss});
      }
    }
    map_point->state().optimized_at_last_time = true;
    if (map_point->state().fixed) {
      optimizer->SetConstant(map_point);
      ++num_fixed_pt;
    }
  }
  // add other map points
  bool skip = false;
  for (auto map_point : *mappoints_need_optimize) {
    if (mpts_actual_in_optimize->count(map_point)) continue;
    if (skip) {
      map_point->state().optimized_at_last_time = false;
      continue;
    }
    bool has_obs_in_optimize = false;
    for (auto kf2obs : map_point->observations()) {
      KeyFrame* kf = kf2obs.first;
      if (filtered_fixed_kfs->count(kf)) {
        if (map_point->state().fixed) continue;
        if (!fixed_kfs_actual_in_optimize->count(kf)) {
          fixed_kfs_actual_in_optimize->insert(kf);
        }
      } else if (kfs_need_optimize->count(kf) && !kfs_actual_in_optimize->count(kf)) {
        kfs_actual_in_optimize->insert(kf);
      }
      has_obs_in_optimize = true;
      for (auto ob : kf2obs.second) {
        float w = kf->LevelFactor(ob.cam_id, ob.feat_id);
        if (kfs_need_optimize->count(kf)) {
          variable_constraint++;
        } else {
          constant_constraint++;
          w *= 3;
        }

        Eigen::Vector2d projection_weight = {KeyFrame::Intrinsic(ob.cam_id).fx() / w,
                                             KeyFrame::Intrinsic(ob.cam_id).fy() / w};
        optimizer->AddProjection(map_point, kf, ob.cam_id, ob.feat_id,
                                {projection_weight, cauchy_loss});
      }
    }

    map_point->state().optimized_at_last_time = true;
    if (has_obs_in_optimize && map_point->state().fixed) {
      optimizer->SetConstant(map_point);
      ++num_fixed_pt;
    }
    mpts_actual_in_optimize->insert(map_point);
    if (variable_constraint + constant_constraint > kMaxOptimizeConstraint) {
      skip = true;
    }
  }
  VLOG(2) << "constant_constraint and  variable_constraint num_fixed_pt " << constant_constraint
            << " " << variable_constraint << " " << num_fixed_pt << " "
            << mpts_actual_in_optimize->size() << " " << mappoints_need_optimize->size();
  return (variable_constraint + constant_constraint);
}

void LocalMapping::AddRelativePoseToProblem(KeyFrameSet* filtered_fixed_kfs,
                              std::unordered_set<KeyFrame*>* kfs_actual_in_optimize,
                              std::unordered_set<KeyFrame*>* fixed_kfs_actual_in_optimize,
                              int coeff, BAOptimizer* optimizer) {
  float pt_constraint_size = coeff;
  Eigen::Matrix6d info = pt_constraint_size * Eigen::Matrix6d::Identity();
  info.block<3, 3>(0, 0) *= 100;
  info.block<3, 3>(3, 3) *= 10;

  int relative_pose_contraints = 0;
  for (auto& opt_frm : *kfs_actual_in_optimize) {
    auto parent = opt_frm->GetParent();
    if (!kfs_actual_in_optimize->count(parent.kf)) {
      if (!filtered_fixed_kfs->count(parent.kf)) continue;
      if (!fixed_kfs_actual_in_optimize->count(parent.kf)) {
        fixed_kfs_actual_in_optimize->insert(parent.kf);
      }
    }
    double delta_time = 1e-9 * std::abs(double(opt_frm->timestamp()) - double(parent.kf->timestamp()));
    Eigen::Matrix6d info2 = info / delta_time;
    dm::math::Rigid3d T01 = parent.kf->odo_pose().Inverse() * opt_frm->odo_pose();
    optimizer->AddRelativePose(parent.kf, opt_frm, T01, {info2, nullptr});
    ++relative_pose_contraints;
  }
  VLOG(2) << "relative_pose_contraints " << relative_pose_contraints;

  // add edges between latest fixed kf and kfs_actual_in_optimize when fixed_kfs_actual_in_optimize
  // is empty, make sure there is a fixed kf in problem at least
  if (fixed_kfs_actual_in_optimize->size() == 0) {
    KeyFrame* kf = *(filtered_fixed_kfs->rbegin());
    for (auto& opt_frm : *kfs_actual_in_optimize) {
      double delta_time = 1e-9 * std::abs(double(opt_frm->timestamp()) - double(kf->timestamp()));
      Eigen::Matrix6d info2 = info / delta_time;
      dm::math::Rigid3d T01 = kf->odo_pose().Inverse() * opt_frm->odo_pose();
      optimizer->AddRelativePose(kf, opt_frm, T01, {info2, nullptr});
      ++relative_pose_contraints;
    }
    fixed_kfs_actual_in_optimize->insert(kf);
    VLOG(2) << "relative_pose_contraints " << relative_pose_contraints;
  }
}

void LocalMapping::ConstructBA(KeyFrameSet* kfs_need_optimize,
                               MapPointOptimizeSet* mappoints_need_optimize,
                               KeyFrameSet* filtered_fixed_kfs,
                               std::unordered_set<KeyFrame*>* kfs_actual_in_optimize,
                               std::unordered_set<KeyFrame*>* fixed_kfs_actual_in_optimize,
                               std::unordered_set<MapPoint*>* mpts_actual_in_optimize) {
  BAOptimizer optimizer(KeyFrame::Intrinsics(), KeyFrame::Extrinsics(), 0.15, 30);

  auto contraints = AddPtToProblem(kfs_need_optimize, mappoints_need_optimize, filtered_fixed_kfs,
                                   kfs_actual_in_optimize, fixed_kfs_actual_in_optimize,
                                   mpts_actual_in_optimize, &optimizer);

  AddRelativePoseToProblem(filtered_fixed_kfs, kfs_actual_in_optimize, fixed_kfs_actual_in_optimize,
                           contraints, &optimizer);

  dm::common::Timer set_constant_t;
  for (auto& kf : *fixed_kfs_actual_in_optimize) {
    optimizer.SetConstant(kf);
  }
  set_constant_t.Toc();
  VLOG(2) << set_constant_t.str();

#ifdef __x86_64__
  TIMER_DECORATOR(optimizer.Solve(), optimizer, 2);
#else
  EndOptimizationCallback callback(this);
  std::vector<ceres::IterationCallback*> callbacks{&callback};

  TIMER_DECORATOR(optimizer.Solve(callbacks), optimizer, 2);
#endif

  VLOG(2) << "lba converge " << (optimizer.summary().termination_type == ceres::CONVERGENCE);
  VLOG(2) << "lba Iterations " << optimizer.summary().iterations.size();
}

void LocalMapping::UpdateIsolatedPt(
    std::map<FrameId, std::map<FeatureId, Eigen::Vector3d>>* isolated_stereo_points,
    std::unordered_set<KeyFrame*>* kfs_actual_in_optimize) {
  for (auto kf_pts : *isolated_stereo_points) {
    KeyFrame* kf = map_->GetKeyFrame(kf_pts.first);
    CHECK(kf != nullptr);
    if (!kfs_actual_in_optimize->count(kf)) continue;
    CameraId cam_id = kf->master_cam_id();
    dm::math::Rigid3d Twc = kf->camera_pose(cam_id);
    for (auto id_pc : kf_pts.second) {
      MapPoint* pt = map_->GetMapPoint(id_pc.first);
      Eigen::Vector3d Pw = Twc * id_pc.second;
      pt->SetWorldPoint3d(Pw);
    }
  }
}

bool LocalMapping::GrabLocalMapInLBA(
    KeyFrameSet* kfs_need_optimize, MapPointOptimizeSet* mappoints_need_optimize,
    KeyFrameSet* filtered_fixed_kfs,
    std::map<FrameId, std::map<FeatureId, Eigen::Vector3d>>* isolated_stereo_points,
    std::unordered_set<KeyFrame*>* kfs_actual_in_optimize,
    std::unordered_set<KeyFrame*>* fixed_kfs_actual_in_optimize,
    std::unordered_set<MapPoint*>* mpts_actual_in_optimize) {
  // grab local map
  // A: Best Covisibility Key Frames of current keyframe
  std::vector<KeyFrame*> neighbor_kfs = current_key_frame_->GetBestCovisibilityKeyFrames(15);
  VLOG(2) << "LocalBA neighbor kfs " << neighbor_kfs.size();
  kfs_need_optimize->insert(neighbor_kfs.begin(), neighbor_kfs.end());

  if (neighbor_kfs.empty()) return false;

  // B: map points in A
  for (auto kf : *kfs_need_optimize) {
    CameraId master_id = kf->master_cam_id();
    std::vector<MapPoint*> map_points = kf->map_points(master_id);
    for (auto mpt : map_points) {
      if (!mpt || mpt->IsBad() || mappoints_need_optimize->count(mpt) != 0) continue;
      // isolated stereo points will not be added to BA
      if (mpt->NumOfObs() == 1) {
        if (recent_add_map_points_.count(mpt->point_id()) == 0) {
          mpt->SetBad();
          map_->EraseMapPointTS(mpt);
          continue;
        }
        Eigen::Vector3d Pw = mpt->WorldPoint3d();
        Eigen::Vector3d Pc = kf->camera_pose(master_id).Inverse() * Pw;
        isolated_stereo_points->operator[](kf->frame_id())[mpt->point_id()] = Pc;
        continue;
      }
      mappoints_need_optimize->insert(mpt);
      if (kf->frame_id() == current_key_frame_->frame_id()) {
        mpts_actual_in_optimize->insert(mpt);
      }
    }
  }

  // C: fixed keyframes
  for (auto mpt : *mappoints_need_optimize) {
    auto kfs_obs = mpt->observations();
    for (auto kf_obs : kfs_obs) {
      if (!kfs_need_optimize->count(kf_obs.first)) {
        filtered_fixed_kfs->insert(kf_obs.first);
      }
    }
  }

  auto first_frm = *(kfs_need_optimize->begin());
  if (first_frm->frame_id() == 0) {
    kfs_need_optimize->erase(kfs_need_optimize->begin());
    filtered_fixed_kfs->insert(first_frm);
  }

  if (filtered_fixed_kfs->empty()) return false;
  return true;
}

void LocalMapping::LocalBA() {
  KeyFrameSet kfs_need_optimize = {current_key_frame_};
  MapPointOptimizeSet mappoints_need_optimize;
  KeyFrameSet filtered_fixed_kfs;
  std::map<FrameId, std::map<FeatureId, Eigen::Vector3d>> isolated_stereo_points;

  std::unordered_set<KeyFrame*> kfs_actual_in_optimize;
  std::unordered_set<KeyFrame*> fixed_kfs_actual_in_optimize;
  std::unordered_set<MapPoint*> mpts_actual_in_optimize;

  bool localmap_validate = GrabLocalMapInLBA(
      &kfs_need_optimize, &mappoints_need_optimize, &filtered_fixed_kfs, &isolated_stereo_points,
      &kfs_actual_in_optimize, &fixed_kfs_actual_in_optimize, &mpts_actual_in_optimize);
  if (!localmap_validate) {
    return;
  }

  ConstructBA(&kfs_need_optimize, &mappoints_need_optimize, &filtered_fixed_kfs,
              &kfs_actual_in_optimize, &fixed_kfs_actual_in_optimize, &mpts_actual_in_optimize);
  // update delta_pose for vio output
  {
    std::unique_lock<std::mutex> lk(latest_drift_mutex_);
    // Twlm_wvio =  Tw(lm)_i * Tw(vio)_i ^-1
    latest_drift_ =
        RigidToVector(current_key_frame_->pose() * current_key_frame_->odo_pose().Inverse());
  }
  TIMER_DECORATOR(MapPointFiltering(mpts_actual_in_optimize, fixed_kfs_actual_in_optimize),
                  MapPointFiltering, 2);

  // update isolated stereo points
  UpdateIsolatedPt(&isolated_stereo_points, &kfs_actual_in_optimize);

  for (auto kf : kfs_actual_in_optimize) {
    kf->set_stable_pose(kf->pose_vec());
  }
  TIMER_DECORATOR(current_key_frame_->UpdateConnections(), UpdateConnections, 2);
}

/**
 * @brief Check redundant keyframes (before current keyframe, only local keyframes)
 * A keyframe is considered redundant if the 90% of the MapPoints it sees, are seen
 * in at least other 3 keyframes
 */
size_t LocalMapping::KeyFrameCulling() {
  std::vector<KeyFrame*> kfs = current_key_frame_->GetBestCovisibilityKeyFrames();
  size_t culling_size = 0;
  auto master_cam = current_key_frame_->master_cam_id();
  auto master_pose = current_key_frame_->camera_pose(master_cam).Inverse();
  std::vector<double> depths;
  for (auto& mpt : current_key_frame_->map_points(master_cam)) {
    if (!mpt || mpt->IsBad()) continue;
    auto cp = master_pose * mpt->WorldPoint3d();
    depths.push_back(cp[2]);
  }
  float dist_threshold = 1.0;
  if (!depths.empty()) {
    std::sort(depths.begin(), depths.end());
    dist_threshold = depths[0.3 * depths.size()];
  }
  VLOG(2) << "KeyFrameCulling dist_threshold " << dist_threshold;
  for (auto kf : kfs) {
    if (kf->frame_id() == 0) {
      continue;
    }
    int n_redundant_observations = 0;
    int n_map_points = 0;
    int obs_threshold = 5;
    const std::vector<MapPoint*> map_points = kf->map_points(kf->master_cam_id());

    for (size_t i = 0, i_end = map_points.size(); i < i_end; ++i) {
      MapPoint* point = map_points[i];
      if (point) {
        ++n_map_points;
        if (point->NumOfObs() > obs_threshold) {
          ++n_redundant_observations;
        }
      }
    }
    if (n_map_points < 100) continue;
    if (n_redundant_observations < 0.9 * n_map_points) continue;

    dm::math::Rigid3d delta_pose = kf->pose().Inverse() * current_key_frame_->pose();
    if (delta_pose.rotation().w() < cos(15 * M_PI / 180.0)) {
      VLOG(2) << "kf culling, reserve by angle " << delta_pose.rotation().w();
      continue;
    }
    if (delta_pose.translation().norm() > dist_threshold) {
      VLOG(2) << "kf culling, reserve by dist " << delta_pose.translation().norm();
      continue;
    }

    map_->EraseKeyFrameTS(kf);
    culling_size++;
  }
  return culling_size;
}


void LocalMapping::ResetLocalMapping() {
  map_->Reset();
  current_key_frame_ = nullptr;
  recent_add_map_points_.clear();
  std::vector<MapPoint*> clean_tmp;
  current_kf_create_map_points_.swap(clean_tmp);

  KeyFrame::public_self_inc_id_ = 0;
  MapPoint::public_self_inc_id_ = 0;
}

void LocalMapping::Run() {
  while (true) {
    if (CheckResetRequest()) {
      Reset();
    }
    if (CheckStopRequest()) {
      Stop();
      break;
    }
    if (CheckPauseRequest()) {
      Pause();
    }
    while (IsPaused()) {
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    if (IsFrameBufferEmpty()) {
      std::this_thread::sleep_for(std::chrono::milliseconds(10));
      continue;
    }
    SetRunningState(true);
    FrameMeta::Ptr candidate_frame = PopFrame();
    if (!candidate_frame) {
      VLOG(2) << "candidate_frame nullptr";
      SetRunningState(false);
      continue;
    }
    Runt(candidate_frame);
    SetRunningState(false);
  }  // end while
  VLOG(2) << "local mapping left to process " << localmapping_buffer_size_;
}

void LocalMapping::Runt(const FrameMeta::Ptr& candidate_frame) {
  // Both entry points execute the same map transaction.
  SyncCameraIntrinsics();
  map_->TryClearKeyFrames();
  map_->TryClearMapPoints();
  // Consume the supplied descriptor-aligned feature observations.
  TIMER_DECORATOR(current_key_frame_ = new KeyFrame(candidate_frame), new_keyframe, 2);

  KeyFrame* last_kf = map_->LastKeyFrame();  // Twi

  if (last_kf) {
    dm::math::Rigid3d T_i_j = last_kf->odo_pose().Inverse() * current_key_frame_->odo_pose();
    dm::math::Rigid3d T_w_j = last_kf->pose() * T_i_j;
    current_key_frame_->set_pose(T_w_j);
    VLOG(2) << "kf interval: " << (current_key_frame_->timestamp() - last_kf->timestamp()) * 1e-6
              << " ms; " << current_key_frame_->timestamp() << " " << last_kf->timestamp();
#ifndef __x86_64__
    last_kf->frame_meta_ = nullptr;
    VLOG(2) << "set last_kf frame_meta nullptr " << last_kf->frame_id();
#endif
  }
  current_key_frame_->set_stable_pose(current_key_frame_->pose_vec());
  bool first_kf = map_->KeyFramesSize() == 0;
  TIMER_DECORATOR(AddKeyFrameToMap(), AddKeyFrameToMap, 2);
  TIMER_DECORATOR(MapPointCulling(), MapPointCulling, 2);
  TIMER_DECORATOR(CreateNewMapPoint(), CreateNewMapPoint, 2);

  // first keyframe added to map should has enough mappoints
  if (first_kf && map_->MapPointsSize() == 0) {
    ResetLocalMapping();
    SetRunningState(false);
    return;
  }

  // Find more matches in neighbor keyframes and fuse point duplications
  TIMER_DECORATOR(FuseWithNeighbors(), FuseWithNeighbors, 2);

  if (map_->KeyFramesSize() > 5) {
    TIMER_DECORATOR(LocalBA(), LocalBA, 2);
    TIMER_DECORATOR(KeyFrameCulling(), KeyFrameCulling, 2);
    VLOG(2) << "after culling map kfs: " << map_->KeyFramesSize();
  }
#ifdef __x86_64__
  DebugMessage("statistics");
#endif
  SetRunningState(false);
  map_->TryClearKeyFrames();
  map_->TryClearMapPoints();
  return;
}

// ========================= code for debug =====================================

cv::Mat GetPatch(cv::Mat input, cv::Point center, int patch_width, int patch_height, int* start_x,
                 int* start_y) {
  int patch_x = center.x - patch_width / 2;
  int patch_y = center.y - patch_height / 2;
  patch_x = std::max(patch_x, 0);
  patch_y = std::max(patch_y, 0);
  patch_x = std::min(patch_x, input.cols - patch_width);
  patch_y = std::min(patch_y, input.rows - patch_height);
  *start_x = patch_x;
  *start_y = patch_y;
  int patch_x_end = std::min(patch_x + patch_width, input.cols);
  int patch_y_end = std::min(patch_y + patch_height, input.rows);
  cv::Rect patch_rect(patch_x, patch_y, patch_width, patch_height);
  cv::Mat patch = input(patch_rect).clone();
  return patch;
}

double LocalMapping::ComputeProjectError(MapPoint* pt3d, double* time_span) {
  double error = 0;
  int num = 0;
  auto kf_obs = pt3d->observations();
  Eigen::Vector3d Pw = pt3d->WorldPoint3d();
  int latest_frame_id = -1;
  int oldest_frame_id = -1;
  for (auto const& kf_ob : kf_obs) {
    KeyFrame* kf = kf_ob.first;
    if (latest_frame_id == -1 || kf->frame_id() > latest_frame_id) {
      latest_frame_id = kf->frame_id();
    }
    if (oldest_frame_id == -1 || kf->frame_id() < oldest_frame_id) {
      oldest_frame_id = kf->frame_id();
    }
    for (auto const& ob : kf_ob.second) {
      dm::math::Rigid3d Twc = kf->camera_pose(ob.cam_id);
      Eigen::Vector3d Pc = Twc.Inverse() * Pw;
      if (Pc[2] > 0) {
        Eigen::Vector2d xy = Eigen::Vector2d(Pc[0] / Pc[2], Pc[1] / Pc[2]);
        double proj_err = (kf->key_points_undistorted_[ob.cam_id][ob.feat_id] - xy).norm() *
                          kf->Intrinsic(ob.cam_id).f();
        error += proj_err;
        ++num;
      } else {
        VLOG(2) << "bad obs " << pt3d->point_id() << " " << kf->frame_id() << " " << ob.cam_id;
      }
    }
  }
  CHECK_NE(num, 0);
  CHECK(latest_frame_id >= oldest_frame_id);
  CHECK(map_->GetKeyFrame(latest_frame_id)->timestamp() >=
        map_->GetKeyFrame(oldest_frame_id)->timestamp());
  *time_span = (map_->GetKeyFrame(latest_frame_id)->timestamp() -
                map_->GetKeyFrame(oldest_frame_id)->timestamp()) *
               1e-6;
  return error / num;
}

void LocalMapping::SaveKeyFrameTraj() {
#ifndef __x86_64__
  PoseWriter lba_writer("/sdcard/Android/data/com.dm.xr.capture/files/lba.tum");
#else
  PoseWriter lba_writer("./lba.tum");
#endif
  auto kfs = map_->key_frames();
  for (auto it = kfs.begin(); it != kfs.end(); ++it) {
    KeyFrame* kf = it->second;
    dm::math::Rigid3d Twi = kf->pose();
    lba_writer.WritePose(kf->timestamp(), Twi);
  }
#ifndef __x86_64__
  PoseWriter msckf_writer("/sdcard/Android/data/com.dm.xr.capture/files/msckf.tum");
#else
  PoseWriter msckf_writer("./msckf.tum");
#endif
  for (auto it = kfs.begin(); it != kfs.end(); ++it) {
    KeyFrame* kf = it->second;
    dm::math::Rigid3d Twi = kf->odo_pose();
    msckf_writer.WritePose(kf->timestamp(), Twi);
  }
}

void LocalMapping::DebugMessage(std::string tag) {
  VLOG(2) << tag << " debug start";
  std::map<FeatureId, MapPoint*> map_points = map_->map_points();
  uint64_t validate_num = 0;
  uint64_t sum = 0;
  double error = 0;
  double span = 0;
  for (auto it = map_points.begin(); it != map_points.end(); ++it) {
    if (it->second->IsBad()) continue;
    CHECK_NE(it->second->observations().size(), 0);
    sum += it->second->observations().size();
    double time_span = 0;
    error += ComputeProjectError(it->second, &time_span);
    span += time_span;
    ++validate_num;
  }
  VLOG(2) << "validate map point in map " << validate_num;
  VLOG(2) << "average obs for a mappoint " << sum / validate_num;
  VLOG(2) << "average projection error(unit: pixel) for a mappoint " << error / validate_num;
  VLOG(2) << "average time span for a map point(uint:ms) " << span / validate_num;

  std::map<FrameId, KeyFrame*> id_kfs = map_->key_frames();
  std::vector<int> nums;
  for (auto it = id_kfs.begin(); it != id_kfs.end(); ++it) {
    KeyFrame* kf = it->second;
    int num_pt = 0;
    std::vector<MapPoint*> master_pts = kf->map_points(kf->master_cam_id());
    for (auto pt : master_pts) {
      if (!pt || pt->IsBad()) {
        continue;
      }
      num_pt++;
    }
    nums.push_back(num_pt);
  }
  std::sort(nums.begin(), nums.end());
  if (id_kfs.size() > 0) {
    VLOG(2) << "min_num pts in kf " << *(nums.begin());
    VLOG(2) << "max_num pts in kf " << *(nums.begin() + (nums.size() - 1));
    VLOG(2) << "average pts in kf "
              << std::accumulate(nums.begin(), nums.end(), 0) / id_kfs.size();
    VLOG(2) << "medium pts in kf " << nums[(nums.size() + 1) / 2];
  }

  VLOG(2) << tag << " debug end";
}

cv::Mat DrawMapPointsInKeyFrame(KeyFrame* kf, CameraId camera_id) {
  auto mpts = kf->map_points(camera_id);
  cv::Mat image = kf->frame_meta()->images[camera_id].clone();
  cv::cvtColor(image, image, cv::COLOR_GRAY2BGR);
  std::map<MapPoint*, cv::Vec3b> colors;
  for (auto& mpt : mpts) {
    if (!mpt || mpt->IsBad()) continue;
    auto kf2obss = mpt->observations();
    std::vector<cv::Point2f> opticals;

    for (auto& kf2obs : kf2obss) {
      int frm_dist = static_cast<int>(kf2obs.first->frame_id()) - static_cast<int>(kf->frame_id());
      auto kpts = kf2obs.first->key_points(camera_id);
      auto ob = kf2obs.second.find(MapPoint::Observation(camera_id));
      if (ob == kf2obs.second.end()) continue;
      opticals.push_back(kpts[ob->feat_id].pt);
    }

    cv::Vec3b line_color;
    if (opticals.size() >= 5)
      line_color = cv::Vec3b(255, 0, 0);
    else if (opticals.size() >= 4)
      line_color = cv::Vec3b(255, 255, 0);
    else if (opticals.size() >= 3)
      line_color = cv::Vec3b(0, 255, 255);
    else if (opticals.size() >= 2)
      line_color = cv::Vec3b(255, 0, 255);
    else
      line_color = cv::Vec3b(0, 0, 255);
    colors[mpt] = line_color;
  }

  auto cam_pose = kf->camera_pose(camera_id);
  for (size_t i = 0; i < mpts.size(); i++) {
    auto mpt = mpts[i];
    if (!mpt || mpt->IsBad()) continue;
    auto kpt2d = kf->key_points(camera_id)[i];
    cv::circle(image, kpt2d.pt, 2, colors[mpt], 2);

    // draw projection errors
    auto wpt3d = mpt->WorldPoint3d();
    auto cpt3d = cam_pose.Inverse() * wpt3d;
    cpt3d.head(2) /= cpt3d[2];
    auto pt2d = KeyFrame::Intrinsic(camera_id).ProjectRayToPoint(cpt3d.head(2));
    cv::Point2f cv_pt2d(pt2d.x(), pt2d.y());

    float err = (pt2d - Eigen::Vector2d(kpt2d.pt.x, kpt2d.pt.y)).norm();

    cv::line(image, kpt2d.pt, cv_pt2d, cv::Vec3b(0, 165, 255));
  }
  return image;
}

cv::Mat DrawProjectionErrors(MapPoint* mpt, size_t patch_size, size_t rows, size_t cols) {
  const auto& kf2obss = mpt->observations();
  cv::Mat canvas = cv::Mat::zeros(rows * patch_size, cols * patch_size, CV_8UC3);
  Eigen::Vector3d wpt = mpt->WorldPoint3d();

  size_t patch_cnt = 0;
  for (auto it = kf2obss.rbegin(); it != kf2obss.rend(); it++) {
    auto kf = it->first;
    for (auto ob : it->second) {
      const auto image = kf->frame_meta()->images[ob.cam_id];
      cv::Point2f pt = kf->key_points(ob.cam_id)[ob.feat_id].pt;
      cv::Point ptt(static_cast<int>(pt.x), static_cast<int>(pt.y));
      auto cpt = kf->camera_pose(ob.cam_id).Inverse() * wpt;
      cpt.head(2) /= cpt[2];
      Eigen::Vector2d p = KeyFrame::Intrinsic(ob.cam_id).ProjectRayToPoint(cpt.head(2));

      int start_x, start_y;
      cv::Mat patch = GetPatch(image, ptt, patch_size, patch_size, &start_x, &start_y);
      cv::cvtColor(patch, patch, cv::COLOR_GRAY2BGR);

      cv::Point p0(ptt.x - start_x, ptt.y - start_y);
      cv::Point p1(p.x() - start_x, p.y() - start_y);

      cv::circle(patch, p0, 2, cv::Vec3b(255, 255, 0), 2);
      cv::line(patch, p0, p1, cv::Vec3b(0, 0, 255));

      size_t patch_y = patch_cnt / cols;
      size_t patch_x = patch_cnt - patch_y * cols;

      int canvas_x = patch_x * patch_size;
      int convas_y = patch_y * patch_size;

      cv::Rect targetRect(canvas_x, convas_y, patch_size, patch_size);

      patch.copyTo(canvas(targetRect));
      patch_cnt++;
      if (patch_cnt >= rows * cols) break;
    }
    if (patch_cnt >= rows * cols) break;
  }
  return canvas;
}

}  // namespace dm::vio::lm
