// Copyright 2020 DeepMirror Inc. All rights reserved.

#include "vio/map/ba_optimizer.h"
#include <unordered_set>
#include <vector>
#include "vio/cost_functions/projection_factor.h"
#include "vio/cost_functions/relative_pose_factor.h"
#include "vio/map/key_frame.h"
#include "vio/map/map_point.h"
#include "vio/pose_local_parameterization.h"

namespace dm::vio::lm {

// use all map point can be seen by the keyframe
void BAOptimizer::AddProjection(KeyFrame* kf, const ProjectionConfig& config) {
  key_frames_[kf->frame_id()] = kf;
  const auto& all_mpts = kf->map_points();
  std::unordered_set<MapPoint*> mpts_set;
  for (const auto& cam2mpts : all_mpts) {
    auto cam_id = cam2mpts.first;
    mpts_set.insert(cam2mpts.second.begin(), cam2mpts.second.end());
  }
  for (const auto& mpt : mpts_set) {
    map_points_[mpt->point_id()] = mpt;
    auto obs = mpt->observations(kf);
    for (const auto& ob : obs.value()) {
      AddProjection(mpt, kf, ob.cam_id, ob.feat_id, config);
    }
  }
}

// use all keyframe who can see the mappoint
void BAOptimizer::AddProjection(MapPoint* mpt, const ProjectionConfig& config) {
  const auto& all_obs = mpt->observations();
  for (const auto& obs : all_obs) {
    const auto& kf = obs.first;
    for (const auto& ob : obs.second) {
      AddProjection(mpt, kf, ob.cam_id, ob.feat_id, config);
    }
  }
}

void BAOptimizer::AddProjection(MapPoint* mpt, KeyFrame* kf, CameraId cam_id,
                                const ProjectionConfig& config) {
  auto ob = mpt->observations(kf, cam_id);
  if (!ob.has_value()) LOG(WARNING) << "map point doesnt have observation";
  AddProjection(mpt, kf, cam_id, ob->feat_id, config);
}

void BAOptimizer::AddProjection(MapPoint* mpt, KeyFrame* kf, CameraId cam_id, size_t feat_idx,
                                const ProjectionConfig& config) {
  const auto& kpt = kf->key_points_undistorted(cam_id)[feat_idx];
  ceres::CostFunction* cost_function =
      new dm::vio::CameraProjectionFactor(camera_extrinsics_[cam_id], kpt, config.weight);
  auto res_id = problem_.AddResidualBlock(cost_function, config.loss_func, kf->pose_data(),
                                          mpt->point3d_data());
  projection_costs_.emplace_back(mpt, kf, cam_id, feat_idx);
  key_frames_[kf->frame_id()] = kf;
  map_points_[mpt->point_id()] = mpt;
  mpt2obs_num_[mpt->point_id()]++;
}

void BAOptimizer::AddRelativePose(KeyFrame* kf0, KeyFrame* kf1, const dm::math::Rigid3d& T01,
                                  const RelativePoseConfig& config) {
  key_frames_[kf0->frame_id()] = kf0;
  key_frames_[kf1->frame_id()] = kf1;
  ceres::CostFunction* cost_function =
      new dm::vio::RelativePoseFactor(T01.rotation(), T01.translation(), config.info);
  auto res_id = problem_.AddResidualBlock(cost_function, config.loss_func, kf0->pose_data(),
                                          kf1->pose_data());
  RelativePoseCostInfo cost_info(kf0, kf1, res_id, &problem_);
  relative_pose_costs_.push_back(cost_info);
}

void BAOptimizer::SetConstant(KeyFrame* kf, const std::vector<int>& constant_indices,
                              bool try_add) {
  if (try_add && !problem_.HasParameterBlock(kf->pose_data())) {
    problem_.AddParameterBlock(kf->pose_data(), 6);
    key_frames_[kf->frame_id()] = kf;
  }
  auto data = kf->pose_data();
#if CERES_VERSION_MAJOR >= 2 && CERES_VERSION_MINOR >= 1
  problem_.SetManifold(data, new ceres::SubsetManifold(6, constant_indices));
#else
  problem_.SetParameterization(data,
                               new ceres::SubsetParameterization(6, constant_indices));
#endif
  //  constant_key_frames_[kf->frame_id()] = kf;
}

void BAOptimizer::SetConstant(MapPoint* mpt, bool try_add) {
  if (try_add && !problem_.HasParameterBlock(mpt->point3d_data())) {
    problem_.AddParameterBlock(mpt->point3d_data(), 3);
  }
  problem_.SetParameterBlockConstant(mpt->point3d_data());
  constant_map_points_[mpt->point_id()] = mpt;
}

void BAOptimizer::SetConstant(KeyFrame* kf, bool try_add) {
  if (try_add && !problem_.HasParameterBlock(kf->pose_data())) {
    problem_.AddParameterBlock(kf->pose_data(), 6);
    key_frames_[kf->frame_id()] = kf;
  }
  problem_.SetParameterBlockConstant(kf->pose_data());
  constant_key_frames_[kf->frame_id()] = kf;
}

bool BAOptimizer::IsConstant(const MapPoint* mpt) {
  return constant_map_points_.count(mpt->point_id());
}

bool BAOptimizer::IsConstant(const KeyFrame* kf) {
  return constant_key_frames_.count(kf->frame_id());
}

void BAOptimizer::Solve(const std::vector<ceres::IterationCallback*>& callbacks) {
  ceres::Manifold* local_parameterization = new dm::vio::PoseManifold();
  for (auto& kf : key_frames_) {
    problem_.SetManifold(kf.second->pose_data(), local_parameterization);
  }
  ceres::Solver::Options options;
  options.linear_solver_type = ceres::DENSE_SCHUR;
  options.max_solver_time_in_seconds = max_solver_time_in_seconds_;
  options.max_num_iterations = max_num_iterations_;

  for (auto callback : callbacks) {
    options.callbacks.push_back(callback);
  }

  ceres::Solve(options, &problem_, &summary_);
}

void BAOptimizer::Reset() {
  problem_ = ceres::Problem();
  key_frames_.clear();
  map_points_.clear();
  constant_key_frames_.clear();
  constant_map_points_.clear();
  sub_constant_key_frames_.clear();
}


//////////////////////////////////////////////////////////////////////////

void BAOptimizerTS::AddProjection(MapPoint* mpt,
                                  KeyFrame* kf,
                                  CameraId cam_id,
                                  size_t feat_idx,
                                  const ProjectionConfig& config) {
  return;
}

void BAOptimizerTS::AddRelativePose(KeyFrame* kf0,
                                    KeyFrame* kf1,
                                    const dm::math::Rigid3d& T01,
                                    const RelativePoseConfig& config) {
  if (!key_frames_.count(kf0->frame_id())) key_frames_[kf0->frame_id()] = kf0->stable_pose();
  if (!key_frames_.count(kf1->frame_id())) key_frames_[kf1->frame_id()] = kf1->stable_pose();
  ceres::CostFunction* cost_function;
  if (four_dof_) {
    cost_function = new dm::vio::LhsRelativePoseFactor(
        T01.rotation(), T01.translation(), config.info);
  } else {
    cost_function = new dm::vio::RelativePoseFactor(
        T01.rotation(), T01.translation(), config.info);
  }
  auto res_id = problem_.AddResidualBlock(cost_function,
                                          config.loss_func,
                                          key_frames_[kf0->frame_id()].data(),
                                          key_frames_[kf1->frame_id()].data());
//  RelativePoseCostInfo cost_info(kf0, kf1, res_id, &problem_);
//  relative_pose_costs_.push_back(cost_info);
}

void BAOptimizerTS::SetConstant(KeyFrame* kf,
                                const std::vector<int>& constant_indices,
                                bool try_add) {
  if (try_add && !key_frames_.count(kf->frame_id())) {
    key_frames_[kf->frame_id()] = kf->stable_pose();
    problem_.AddParameterBlock(key_frames_[kf->frame_id()].data(), 6);
  }
#if CERES_VERSION_MAJOR >= 2 && CERES_VERSION_MINOR >= 1
  auto data = key_frames_[kf->frame_id()].data();
  problem_.SetManifold(data, new ceres::SubsetManifold(6, constant_indices));
#else
  problem_.SetParameterization(data, new ceres::SubsetParameterization(6, constant_indices));
#endif
}

void BAOptimizerTS::SetConstant(MapPoint* mpt, bool try_add) {
  return;
}

void BAOptimizerTS::SetConstant(KeyFrame* kf, bool try_add) {
  if (try_add && !key_frames_.count(kf->frame_id())) {
    key_frames_[kf->frame_id()] = kf->stable_pose();
    problem_.AddParameterBlock(key_frames_[kf->frame_id()].data(), 6);
  }
  problem_.SetParameterBlockConstant(kf->pose_data());
  constant_key_frames_.insert(kf->frame_id());
}

bool BAOptimizerTS::IsConstant(const MapPoint* mpt) {
  return constant_map_points_.count(mpt->point_id());
}

bool BAOptimizerTS::IsConstant(const KeyFrame* kf) {
  return constant_key_frames_.count(kf->frame_id());
}

void BAOptimizerTS::Solve() {
  ceres::Manifold* local_parameterization = nullptr;
  if (four_dof_) {
    local_parameterization = new dm::vio::YawPoseManifold();
  } else {
    local_parameterization = new dm::vio::PoseManifold();
  }
  for (auto& kf : key_frames_) {
    problem_.SetManifold(kf.second.data(), local_parameterization);
  }
  if (constant_key_frames_.empty()) {
    VLOG(1) << "LoopClosure fix frame: " << key_frames_.begin()->first;
    problem_.SetParameterBlockConstant(key_frames_.begin()->second.data());
  }
  ceres::Solver::Options options;
  options.linear_solver_type = ceres::DENSE_SCHUR;
  options.max_solver_time_in_seconds = max_solver_time_in_seconds_;
  options.max_num_iterations = max_num_iterations_;
  ceres::Solve(options, &problem_, &summary_);
}

void BAOptimizerTS::Reset() {
  problem_ = ceres::Problem();
  key_frames_.clear();
  map_points_.clear();
  constant_key_frames_.clear();
  constant_map_points_.clear();
  sub_constant_key_frames_.clear();
}
}  // namespace dm::vio::lm
