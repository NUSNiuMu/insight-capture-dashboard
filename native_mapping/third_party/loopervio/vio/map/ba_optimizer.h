// Copyright 2020 DeepMirror Inc. All rights reserved.
#ifndef VIO_MAP_BA_OPTIMIZER_H_
#define VIO_MAP_BA_OPTIMIZER_H_

#include <unordered_map>
#include <vector>
#include <map>
#include <unordered_set>
#include "ceres/ceres.h"
#include "vio/map/key_frame.h"
#include "vio/map/map_point.h"

namespace dm::vio::lm {

struct ProjectionConfig {
  Eigen::Vector2d weight;
  ceres::LossFunction* loss_func = nullptr;
};

struct RelativePoseConfig {
  Eigen::Matrix<double, 6, 6> info;
  ceres::LossFunction* loss_func = nullptr;
};

struct CostInfo {
 public:
  CostInfo() {}
  CostInfo(size_t _res_size, ceres::ResidualBlockId _block_id, ceres::Problem* _problem)
      : res_size(_res_size), block_id(_block_id), problem(_problem) {}
  size_t res_size;
  ceres::ResidualBlockId block_id = nullptr;
  ceres::Problem* problem = nullptr;
  virtual bool Evaluate(Eigen::VectorXd* res) const {
    *res = Eigen::VectorXd(res_size);
    double cost;
    bool state = problem->EvaluateResidualBlock(block_id, false, &cost, res->data(), nullptr);
    return state;
  }
};

struct ProjectCostInfo : public CostInfo {
  ProjectCostInfo(MapPoint* _mpt, KeyFrame* _kf, CameraId _cam_id, size_t _feat_idx,
                  ceres::ResidualBlockId _block_id, ceres::Problem* _problem)
      : CostInfo(2, _block_id, _problem),
        mpt(_mpt),
        kf(_kf),
        cam_id(_cam_id),
        feat_idx(_feat_idx) {}
  ProjectCostInfo(MapPoint* _mpt, KeyFrame* _kf, CameraId _cam_id, size_t _feat_idx)
      : CostInfo(2, nullptr, nullptr), mpt(_mpt), kf(_kf), cam_id(_cam_id), feat_idx(_feat_idx) {}
  MapPoint* mpt;
  KeyFrame* kf;
  CameraId cam_id;
  size_t feat_idx;
};

struct RelativePoseCostInfo : public CostInfo {
  RelativePoseCostInfo(KeyFrame* _kf0, KeyFrame* _kf1, ceres::ResidualBlockId _block_id,
                       ceres::Problem* _problem)
      : CostInfo(6, _block_id, _problem), kf0(_kf0), kf1(_kf1) {}
  RelativePoseCostInfo(KeyFrame* _kf0, KeyFrame* _kf1)
      : CostInfo(2, nullptr, nullptr), kf0(_kf0), kf1(_kf1) {}
  KeyFrame* kf0;
  KeyFrame* kf1;
};

enum class CeresLossFunctionType { TRIVIAL, SOFT_L1, CAUCHY };

class BAOptimizer {
 public:
  BAOptimizer() {}
  BAOptimizer(const std::vector<dm::common::ImageUndistortion>& intrinsics,
              const std::vector<dm::math::Rigid3d>& extrinsics, float max_solver_time_in_seconds,
              size_t max_num_iterations)
      : image_intrinsics_(intrinsics),
        camera_extrinsics_(extrinsics),
        max_solver_time_in_seconds_(max_solver_time_in_seconds),
        max_num_iterations_(max_num_iterations) {}
  // use all map point can be seen by the keyframe
  void AddProjection(KeyFrame* kf, const ProjectionConfig& config);

  // use all keyframe who can see the mappoint
  void AddProjection(MapPoint* mpt, const ProjectionConfig& config);

  void AddProjection(MapPoint* mpt, KeyFrame* kf, CameraId cam_id, const ProjectionConfig& config);

  void AddProjection(MapPoint* mpt, KeyFrame* kf, CameraId cam_id, size_t feat_idx,
                     const ProjectionConfig& config);

  void AddRelativePose(KeyFrame* kf0, KeyFrame* kf1, const dm::math::Rigid3d& T01,
                       const RelativePoseConfig& config);

  void SetConstant(KeyFrame* kf, const std::vector<int>& constant_indices, bool try_add = false);

  void SetConstant(MapPoint* mpt, bool try_add = false);

  void SetConstant(KeyFrame* kf, bool try_add = false);

  bool IsConstant(const MapPoint* mpt);

  bool IsConstant(const KeyFrame* kf);

  void Solve(const std::vector<ceres::IterationCallback*>& callbacks =
  std::vector<ceres::IterationCallback*>());

  const ceres::Solver::Summary& summary() const { return summary_; }

  void Reset();

  std::vector<ProjectCostInfo>& projection_costs() { return projection_costs_; }

  const std::vector<ProjectCostInfo>& projection_costs() const { return projection_costs_; }

  std::vector<RelativePoseCostInfo>& relative_pose_costs() { return relative_pose_costs_; }

  const std::vector<RelativePoseCostInfo>& relative_pose_costs() const {
    return relative_pose_costs_;
  }

 private:
  std::unordered_map<FrameId, KeyFrame*> key_frames_;
  std::unordered_map<FeatureId, MapPoint*> map_points_;
  std::unordered_map<FeatureId, size_t> mpt2obs_num_;

  std::unordered_map<FrameId, KeyFrame*> constant_key_frames_;
  std::unordered_map<FeatureId, MapPoint*> constant_map_points_;
  std::unordered_map<FrameId, KeyFrame*> sub_constant_key_frames_;

  ceres::Problem problem_;

  std::vector<dm::common::ImageUndistortion> image_intrinsics_;
  std::vector<dm::math::Rigid3d> camera_extrinsics_;

  float max_solver_time_in_seconds_ = 0.05;
  size_t max_num_iterations_ = 30;
  ceres::Solver::Summary summary_;

  std::vector<ProjectCostInfo> projection_costs_;
  std::vector<RelativePoseCostInfo> relative_pose_costs_;
};

class BAOptimizerTS {
 public:
  BAOptimizerTS() {}
  BAOptimizerTS(
      const std::vector<dm::common::ImageUndistortion>& intrinsics,
      const std::vector<dm::math::Rigid3d>& extrinsics,
      float max_solver_time_in_seconds,
      size_t max_num_iterations) :
      image_intrinsics_(intrinsics), camera_extrinsics_(extrinsics),
      max_solver_time_in_seconds_(max_solver_time_in_seconds),
      max_num_iterations_(max_num_iterations) {}

  void AddProjection(MapPoint* mpt,
                     KeyFrame* kf,
                     CameraId cam_id,
                     size_t feat_idx,
                     const ProjectionConfig& config);

  void AddRelativePose(KeyFrame* kf0,
                       KeyFrame* kf1,
                       const dm::math::Rigid3d& T01,
                       const RelativePoseConfig& config);

  void SetConstant(KeyFrame* kf, const std::vector<int>& constant_indices, bool try_add = false);

  void SetConstant(MapPoint* mpt, bool try_add = false);

  void SetConstant(KeyFrame* kf, bool try_add = false);

  bool IsConstant(const MapPoint* mpt);

  bool IsConstant(const KeyFrame* kf);

  void Solve();

  void Reset();

  const ceres::Solver::Summary& summary() const { return summary_; }

  std::vector<ProjectCostInfo>& projection_costs() { return projection_costs_; }

  const std::vector<ProjectCostInfo>& projection_costs() const { return projection_costs_; }

  std::vector<RelativePoseCostInfo>& relative_pose_costs() { return relative_pose_costs_; }

  const std::vector<RelativePoseCostInfo>& relative_pose_costs() const {
    return relative_pose_costs_;
  }

  const std::map<FrameId, Eigen::Vector6d>& key_frames() const { return key_frames_; }

 private:
  std::map<FrameId, Eigen::Vector6d> key_frames_;
  std::unordered_map<FeatureId, Eigen::Vector3d> map_points_;
  std::unordered_map<FeatureId, size_t> mpt2obs_num_;

  std::unordered_set<FrameId> constant_key_frames_;
  std::unordered_set<FeatureId> constant_map_points_;
  std::unordered_set<FrameId> sub_constant_key_frames_;

  ceres::Problem problem_;

  std::vector<dm::common::ImageUndistortion> image_intrinsics_;
  std::vector<dm::math::Rigid3d> camera_extrinsics_;

  float max_solver_time_in_seconds_ = 0.5;
  size_t max_num_iterations_ = 30;
  ceres::Solver::Summary summary_;

  bool four_dof_ = true;

  std::vector<ProjectCostInfo> projection_costs_;
  std::vector<RelativePoseCostInfo> relative_pose_costs_;
};
}  // namespace dm::vio::lm
#endif  // VIO_MAP_BA_OPTIMIZER_H_
