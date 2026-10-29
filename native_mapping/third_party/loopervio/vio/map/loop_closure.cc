// Copyright 2020 DeepMirror Inc. All rights reserved.
#include "vio/map/loop_closure.h"
#include <memory>
#include <vector>
#include <Eigen/Eigenvalues>
#include "math/pnp.h"
#include "ransac/loransac.h"
#include "vio/cost_functions/projection_factor.h"
#include "vio/cost_functions/relative_pose_factor.h"
#include "vio/map/ba_optimizer.h"
#include "vio/map/local_mapping.h"
#include "vio/pose_local_parameterization.h"
#include "vio/triangulation/triangulation.h"

namespace dm::vio::lm {

LoopClosure::LoopClosure(Map* map, LocalMapping* local_mapping)
    : map_(map), local_mapping_(local_mapping) {
}

void LoopClosure::Run() {
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
    // todo: maybe need to sleep several ms to wait lba

    KeyFrame* cur_kf = PopLoopBuffer();
    if (!cur_kf) continue;
    Process(cur_kf);
  }  // end while()
}  // end Run()

bool LoopClosure::Process(KeyFrame* cur_kf) {
    VLOG(2) << "LoopClosure cur kf id: " << cur_kf->frame_id();
    std::string loop_info =
        "LoopClosure: current keyframe [id " + std::to_string(cur_kf->frame_id()) + "], ";

    static common::Timer timer0("Loop FindSimilarFrames");
    timer0.Tic();

    // only use bow
    float bow_threshold = ComputeBowMatchThreshold(cur_kf);

    // only use bow
    auto candidates = map_->FindSimilarFrames(cur_kf, 1, 20);
    timer0.Toc();
    VLOG(2) << timer0;

    if (candidates.empty() || candidates[0].score < bow_threshold) {
      loop_info += "can not find a similar keyframe.\n";
      std::lock_guard<std::mutex> lg(loop_info_mutex_);
      loop_info_ = loop_info;
      return false;
    }
    VLOG(2) << "LoopClosure cur kf id ==> score cmp: " << cur_kf->frame_id() << " "
            << candidates[0].kf->frame_id() << " | " << candidates[0].score << " " << bow_threshold
            << " || " << TimestampFormat(cur_kf->timestamp() - candidates[0].kf->timestamp());
    loop_info +=
        "find a similar keyframe [id " + std::to_string(candidates[0].kf->frame_id()) + "], ";

    auto loop_frm0 = candidates[0].kf;
    static common::Timer timer1("Loop SolveLoopPose");
    timer1.Tic();

    // use map points and feat descriptor in loop kf
    auto Twc0 = SolveLoopPose(cur_kf, loop_frm0, 10);  // return T_cur_query
    timer1.Toc();
    VLOG(2) << timer1;

    if (!Twc0.has_value()) {
      map_->set_enable_erase_flag(true);
      loop_info += "but solve loop pose failed.\n";
      std::lock_guard<std::mutex> lg(loop_info_mutex_);
      loop_info_ = loop_info;
      return false;
    }
    auto loop_neibours = loop_frm0->GetBestCovisibilityKeyFrames(1);
    if (loop_neibours.empty()) return false;
    auto loop_frm2 = loop_neibours[0];
    auto Twc2 = SolveLoopPose(cur_kf, loop_frm2, 10);  // return T_cur_query
    if (!Twc2.has_value()) {
      map_->set_enable_erase_flag(true);
      loop_info += "but solve loop pose with covisibility keyframes failed.\n";
      std::lock_guard<std::mutex> lg(loop_info_mutex_);
      loop_info_ = loop_info;
      return false;
    }

    const double translation_agreement=(Twc0->translation()-Twc2->translation()).norm();
    const double rotation_agreement=Twc0->rotation().angularDistance(Twc2->rotation());
    if (translation_agreement>.05 || rotation_agreement>3.*M_PI/180.) {
      loop_info += "but solve loop pose with inconsistency.\n";
      std::lock_guard<std::mutex> lg(loop_info_mutex_);
      loop_info_ = loop_info;
      map_->set_enable_erase_flag(true);
      return false;
    }

    VLOG(2) << "pnp pose agreement: " << translation_agreement << " / " << rotation_agreement;
    static common::Timer timer2("Loop OptimizeLoopPose");
    timer2.Tic();
    // use map points in local frames
    auto opt_loop_pose = OptimizeLoopPose(cur_kf, loop_frm0, Twc0.value());
    timer2.Toc();
    VLOG(2) << timer2;
    if (!opt_loop_pose.has_value()) {
      map_->set_enable_erase_flag(true);
      loop_info += "but optimize loop pose failed.\n";
      std::lock_guard<std::mutex> lg(loop_info_mutex_);
      loop_info_ = loop_info;
      return false;
    }
    VLOG(2) << "to output loop info";
    auto
        opt_Tlc = RigidFromVector(loop_frm0->stable_pose()).Inverse() * opt_loop_pose.value();
    auto
        delta_pose = RigidFromVector(loop_frm0->stable_pose()).Inverse()
        * RigidFromVector(cur_kf->stable_pose());
//    dm::math::Rigid3d gt_pose0, gt_pose1;
//    bool s0 = pose_server_->GetPose(candidates[0].kf->raw_timestamp(), &gt_pose0);
//    bool s1 = pose_server_->GetPose(cur_kf->raw_timestamp(), &gt_pose1);
//    auto gt_delta_pose = gt_pose0.Inverse() * gt_pose1;
//    auto stereo_delta_test = SolveLoopPose2(cur_kf, candidates[0].kf);
    auto diff_rigid = opt_Tlc * delta_pose.Inverse();
    VLOG(2) << "pose diff: " << diff_rigid.translation().norm() << " | " << diff_rigid;
//    auto diff2 = opt_Tlc * gt_delta_pose.Inverse();
//    auto diff3 = delta_pose * gt_delta_pose.Inverse();
//
//    VLOG(2)<<"pose diff: "<<diff_rigid.translation().norm()<<" | "<<diff_rigid <<"\n | gt "
//    <<diff2.translation().norm()<<" "<<diff2
//    <<"\n"<<diff3.translation().norm()<<" "<<diff3;

    // do 4dof optimization
    cur_kf->AddLoopInfo(LoopInfo(cur_kf->frame_id(), loop_frm0->frame_id(), opt_Tlc));

    // pause lba and build pose graph problem,
    // todo: dont real erase keyframes in lba
    RunPoseGraph();

    VLOG(2) << "to update keyframe pose";
    UpdateKeyFramePoses();
    // Bind the two map regions after correction instead of retaining duplicates.
    std::vector<MapPoint*> loop_points;
    for(auto point:loop_frm0->map_points(loop_frm0->master_cam_id()))
      if(point && !point->IsBad() && point->NumOfObs()>=3 && point->state().refined)
        loop_points.push_back(point);
    ORBMatcher fusion(60,100,.8);
    fusion.Fuse(cur_kf,loop_points,3.,"loop fusion");
    cur_kf->UpdateConnections();
    loop_frm0->UpdateConnections();
    VLOG(2) << "end update keyframe pose";
    loop_info += "and optimize loop pose graph successfully.\n";
    std::lock_guard<std::mutex> lg(loop_info_mutex_);
    loop_info_ = loop_info;
    map_->set_enable_erase_flag(true);
  return true;
}

std::optional<dm::math::Rigid3d> LoopClosure::Localize(KeyFrame* query) {
  evidence_={};
  auto candidates = map_->RetrieveSimilarFrames(query->bow_vecs(query->master_cam_id()), 5);
  std::vector<std::pair<dm::math::Rigid3d,Evidence>> hypotheses;
  for (const auto& candidate : candidates) {
    auto initial = SolveLoopPose(query, candidate.kf, 10);
    if (!initial) continue;
    auto refined = OptimizeLoopPose(query, candidate.kf, *initial);
    if (!refined) continue;
    evidence_.reference=candidate.kf;
    const auto camera_pose=*refined*KeyFrame::Extrinsic(query->master_cam_id());
    for(const auto& previous:hypotheses) {
      const auto other=previous.first*KeyFrame::Extrinsic(query->master_cam_id());
      if((camera_pose.translation()-other.translation()).norm()>.01 ||
         camera_pose.rotation().angularDistance(other.rotation())>M_PI/180.) continue;
      if(previous.second.rms_px<evidence_.rms_px) {
        evidence_=previous.second; refined=previous.first;
      }
      evidence_.verified_references=2;
      return refined;
    }
    hypotheses.emplace_back(*refined,evidence_);
  }
  return std::nullopt;
}

std::optional<dm::math::Rigid3d> LoopClosure::SolveLoopPose(KeyFrame* query_kf,
                                                            KeyFrame* loop_kf,
                                                            int min_inlier) {
  CHECK_NE(query_kf, nullptr);
  CHECK_NE(loop_kf, nullptr);

  std::vector<MapPoint*> matched_mpts;
  ORBMatcher matcher(50, 150, 0.85, true);
  map_->AcquireMptMutex();
  auto matches = matcher.SearchByBfForLoop(query_kf, loop_kf, &matched_mpts);
  for (auto mpt : matched_mpts) {
    if (mpt) mpt->set_forced_retention(true);
  }
  map_->ReleaseMptMutex();

  // solve pnp, use ransac ?
  std::vector<Eigen::Vector3d> pt3ds;
  std::vector<Eigen::Vector2d> pt2ds;
  std::vector<cv::Point2f> kpt2ds;
  std::vector<size_t> mpt_indices;
  auto query_master_cam = query_kf->master_cam_id();
  for (size_t i = 0; i < matched_mpts.size(); i++) {
    if (!matched_mpts[i] || matched_mpts[i]->IsBad()) continue;
    pt3ds.push_back(matched_mpts[i]->WorldPoint3d());
    pt2ds.push_back(query_kf->key_points_undistorted(query_master_cam)[i]);
    mpt_indices.push_back(i);
    kpt2ds.push_back(query_kf->key_points(query_master_cam)[i].pt);
  }

  RANSACOptions options;
  options.max_error = 3.0 / KeyFrame::Intrinsic(query_kf->master_cam_id()).f();
  LORANSAC<P3PEstimator, EPNPEstimator> ransac_pnp(options);  // EPNPEstimator is too slow
  auto report = ransac_pnp.Estimate(pt2ds, pt3ds);
  auto inlier_mask = report.inlier_mask;
  auto inlier_num = std::accumulate(inlier_mask.begin(), inlier_mask.end(), 0);
  VLOG(2) << "pnp report: " << report.success << " | " << inlier_mask.size() << " " << inlier_num;

  // todo: check inliers
  if (!report.success || inlier_num < min_inlier) return std::nullopt;

  if (false) {
    auto loop_cam = loop_kf->master_cam_id();
    std::vector<cv::DMatch> pnp_matches;
    auto loop_mpts = loop_kf->map_points(loop_cam);
    for (int j = 0; j < pt3ds.size(); j++) {
      if (!inlier_mask[j]) continue;
      for (int i = 0; i < loop_mpts.size(); i++) {
        if (!loop_mpts[i] || loop_mpts[i]->IsBad()) continue;
        if (loop_mpts[i] == matched_mpts[mpt_indices[j]]) {
          pnp_matches.emplace_back(mpt_indices[j], i, 0);
        }
      }
    }
    cv::Mat img = DrawMatches(query_kf, loop_kf, pnp_matches);
    //cv::imshow("loop img", img);
    //cv::waitKey();
  }

  Eigen::Matrix4d Tcw_mat = Eigen::Matrix4d::Identity();
  Tcw_mat.block<3, 4>(0, 0) = report.model;
  dm::math::Rigid3d Tcw = dm::math::Rigid3d::FromMatrix(Tcw_mat);
  // try to refine pose
  for (int j = 0; j < pt3ds.size(); j++) {
    if (!inlier_mask[j]) matched_mpts[mpt_indices[j]] = nullptr;
  }
  dm::math::Rigid3d Twi = Tcw.Inverse() * KeyFrame::Extrinsic(query_kf->master_cam_id()).Inverse();
  return Twi;
}

std::optional<dm::math::Rigid3d> LoopClosure::SolveLoopPose2(KeyFrame* query_kf,
                                                             KeyFrame* loop_kf) {
  auto loop_kf2 = new KeyFrame(*loop_kf);
  loop_kf2->map_points(0) = std::vector<MapPoint*>(loop_kf2->FeatureSize(0), nullptr);
  loop_kf2->map_points(1) = std::vector<MapPoint*>(loop_kf2->FeatureSize(1), nullptr);
  size_t s_loop_cam = loop_kf2->slave_cam_id();
  size_t m_loop_cam = loop_kf2->master_cam_id();
  // todo: try triangulate loop kf
  Triangulation triangulation;
  auto s2m_matches = loop_kf->Slave2MasterMatches(s_loop_cam);
  auto& slave_kpts = loop_kf2->key_points_undistorted(s_loop_cam);
  auto& master_kpts = loop_kf2->key_points_undistorted(m_loop_cam);
  dm::math::Rigid3d slave_pose = loop_kf2->camera_pose(s_loop_cam);
  dm::math::Rigid3d master_pose = loop_kf2->camera_pose(m_loop_cam);
  size_t tri_cnt = 0;
  for (int i = 0; i < s2m_matches.s2m_matches.size(); i++) {
    int j = s2m_matches.s2m_matches[i];
    std::vector<Eigen::Vector2d> pt2ds;
    pt2ds.emplace_back(slave_kpts[i]);
    pt2ds.emplace_back(master_kpts[j]);

    auto wpt3d = triangulation.triangulate(pt2ds, {slave_pose, master_pose});
    if (!wpt3d.has_value()) continue;
    MapPoint* mpt = new MapPoint(wpt3d.value(), nullptr);
    loop_kf2->map_points(m_loop_cam)[j] = mpt;
    tri_cnt++;
  }
  return SolveLoopPose(query_kf, loop_kf2);
}

std::optional<dm::math::Rigid3d> LoopClosure::OptimizeLoopPose(KeyFrame* query_kf,
                                                               KeyFrame* loop_kf,
                                                               const math::Rigid3d& query_Twi) {
  MapPointSet local_mpts;
  KeyFrameSet loop_kfs = {loop_kf};
  auto kfs = loop_kf->GetBestCovisibilityKeyFrames(5);
  if (!kfs.empty()) loop_kfs.insert(kfs.begin(), kfs.end());
  map_->AcquireMptMutex();
  for (auto& kf : kfs) {
    auto mpts = kf->map_points(kf->master_cam_id());
    for (auto& mpt : mpts) {
      if (!mpt || mpt->IsBad()) continue;
      mpt->set_forced_retention(true);
      local_mpts.insert(mpt);
    }
  }
  map_->ReleaseMptMutex();

  ORBMatcher matcher(100, 150, 0.85);
  std::vector<MapPoint*> matched_mpts;
  matcher.SearchByProjectionForLoop(query_kf, query_Twi, local_mpts, &matched_mpts, 20);
  return RefinePose(query_kf,matched_mpts,query_Twi,40,.5);
}

std::optional<dm::math::Rigid3d> LoopClosure::RefinePose(KeyFrame* query_kf,
    const std::vector<MapPoint*>& matched_mpts, const math::Rigid3d& query_Twi,
    size_t minimum_inliers, double minimum_ratio) {
  evidence_={};
  size_t matched_cnt=0;
  for(auto point:matched_mpts)
    matched_cnt+=point && !point->IsBad() && point->NumOfObs()>=3 && point->state().refined;
  if(matched_cnt<minimum_inliers) return std::nullopt;

  ceres::Problem problem;
  auto query_cam = query_kf->master_cam_id();
  auto xys = query_kf->key_points_undistorted(query_cam);
  Eigen::Vector6d opt_pose = RigidToVector(query_Twi);
  ceres::Manifold* local_parameterization = new dm::vio::PoseManifold();
  problem.AddParameterBlock(opt_pose.data(), 6, local_parameterization);
  auto focus_len = KeyFrame::Intrinsic(query_cam).f();
  ceres::LossFunction* loss = new ceres::HuberLoss(1.0 / focus_len);

  auto extrinsic = KeyFrame::Extrinsic(query_cam);

  std::vector<Eigen::Vector3d> pt3ds(matched_mpts.size());
  std::vector<ceres::ResidualBlockId> residuals(matched_mpts.size(), nullptr);

  for (size_t i = 0; i < matched_mpts.size(); i++) {
    if (!matched_mpts[i] || matched_mpts[i]->IsBad() ||
        matched_mpts[i]->NumOfObs()<3 || !matched_mpts[i]->state().refined) continue;
    pt3ds[i] = matched_mpts[i]->WorldPoint3d();
    ceres::CostFunction* projection_cost_function = new dm::vio::CameraProjectionFactor(
        extrinsic, xys[i], Eigen::Vector2d(1.0, 1.0));
    residuals[i] = problem.AddResidualBlock(projection_cost_function, loss, opt_pose.data(),
                             pt3ds[i].data());
    problem.SetParameterBlockConstant(pt3ds[i].data());
  }

  ceres::Solver::Options options_pnp;
  options_pnp.linear_solver_type = ceres::DENSE_SCHUR;
  options_pnp.max_solver_time_in_seconds = 1.0;
  options_pnp.max_num_iterations = 20;

  ceres::Solver::Summary summary;
  ceres::Solve(options_pnp, &problem, &summary);
  evidence_.reason="unusable_pose_solution";
  if(!summary.IsSolutionUsable()) return std::nullopt;

  // Refit only geometrically supported correspondences after the robust solve.
  auto first_camera_from_map = (RigidFromVector(opt_pose) * extrinsic).Inverse();
  size_t retained=0;
  for (size_t i=0; i<residuals.size(); ++i) {
    if(!residuals[i]) continue;
    Eigen::Vector3d c = first_camera_from_map * pt3ds[i];
    double error=(c.head(2)/c[2]-xys[i]).norm()*focus_len;
    if(c[2]<=0 || !std::isfinite(error) || error>3.0) {
      problem.RemoveResidualBlock(residuals[i]); residuals[i]=nullptr;
    } else ++retained;
  }
  evidence_.reason="insufficient_retained_landmarks";
  if(retained<minimum_inliers) return std::nullopt;
  ceres::Solve(options_pnp, &problem, &summary);
  evidence_.reason="unusable_pose_solution";
  if(!summary.IsSolutionUsable()) return std::nullopt;

  math::Rigid3d opt_cam_pose = RigidFromVector(opt_pose) * extrinsic;
  math::Rigid3d opt_Tcw = opt_cam_pose.Inverse();
  size_t good_cnt = 0;
  double squared_error=0; std::set<int> cells;
  std::vector<std::pair<size_t, FeatureId>> observations;
  std::vector<Eigen::Vector3d> camera_points;
  for (size_t i = 0; i < matched_mpts.size(); i++) {
    if (!residuals[i]) continue;
    Eigen::Vector3d cpt = opt_Tcw * matched_mpts[i]->WorldPoint3d();
    float err = (cpt.head(2)/cpt[2] - xys[i]).norm() * focus_len;
    if (cpt[2] <= 0 || !std::isfinite(err) || err > 3.0) continue;
    good_cnt++;
    observations.emplace_back(i,matched_mpts[i]->point_id());
    camera_points.push_back(cpt);
    squared_error+=err*err;
    const auto pixel=query_kf->key_points(query_cam)[i].pt;
    const auto& intrinsic=KeyFrame::Intrinsic(query_cam);
    cells.insert(int(pixel.y*4/intrinsic.height())*4+int(pixel.x*4/intrinsic.width()));
  }
  evidence_={matched_cnt,good_cnt,cells.size(),good_cnt?std::sqrt(squared_error/good_cnt):0.,nullptr,std::move(observations)};
  evidence_.reason="insufficient_image_support";
  if(good_cnt<minimum_inliers || good_cnt<matched_cnt*minimum_ratio || cells.size()<4) return std::nullopt;
  double depth=0.;
  for(const auto& p:camera_points) depth+=p[2]/good_cnt;
  Eigen::Matrix6d information=Eigen::Matrix6d::Zero();
  for(const auto& p:camera_points) {
    Eigen::Matrix<double,2,3> projection;
    projection<<1./p[2],0.,-p[0]/(p[2]*p[2]),0.,1./p[2],-p[1]/(p[2]*p[2]);
    Eigen::Matrix3d cross;
    cross<<0.,-p[2],p[1],p[2],0.,-p[0],-p[1],p[0],0.;
    Eigen::Matrix<double,2,6> jacobian;
    jacobian.leftCols<3>()=projection*depth;
    jacobian.rightCols<3>()=projection*cross;
    information+=jacobian.transpose()*jacobian;
  }
  // Balance translation by scene depth and evaluate at the camera, not the world origin.
  Eigen::SelfAdjointEigenSolver<Eigen::Matrix6d> spectrum(information);
  evidence_.reason="degenerate_pose_geometry";
  if(spectrum.info()!=Eigen::Success) return std::nullopt;
  evidence_.condition_ratio=spectrum.eigenvalues()[0]/spectrum.eigenvalues()[5];
  if(!std::isfinite(evidence_.condition_ratio) || evidence_.condition_ratio<1e-6) return std::nullopt;
  evidence_.reason="accepted";
  return RigidFromVector(opt_pose);
}

void LoopClosure::UpdateKeyFramePoses() {
  // Serialized with local mapping by the request owner.
  static common::Timer timer3("Loop UpdateKeyFramePoses");
  VLOG(2) << "LoopClosure update poses: " << optimizer_.key_frames().size();
  timer3.Tic();
  map_->UpdateKeyFramePoses(optimizer_.key_frames());
  timer3.Toc();
  VLOG(2) << timer3;
  // todo: delete map points and keyframes which are to deleted
}

void LoopClosure::RunPoseGraph() {
  // Serialized with local mapping by the request owner.
  static common::Timer timer4("Loop RunPoseGraph build");
  timer4.Tic();
  optimizer_.Reset();
  auto loss_function = new ceres::HuberLoss(0.05);
  size_t loop_edge_cnt = 0;
  Eigen::Matrix6d info = Eigen::Matrix6d::Identity();
  info.block<3, 3>(0, 0) *= 100;

  for (auto& id2kf : map_->key_frames()) {
    auto kf = id2kf.second;
    auto parent = kf->GetParent().kf;
    if (!parent) {
      LOG(WARNING) << "LoopClosure kf dosnt have parent! " << id2kf.first;
      continue;
    }
    auto sp0 = parent->stable_pose();
    auto sp1 = kf->stable_pose();
    dm::math::Rigid3d T01 = RigidFromVector(sp0).Inverse() * RigidFromVector(sp1);
    optimizer_.AddRelativePose(parent, kf, T01, {info, nullptr});

    // auto neibours
    auto weighted_neibours = kf->GetPreBestCovisibilityKeyFramesWithWeight(10);
    for (auto w2kf : weighted_neibours) {
      if (w2kf.kf == parent) continue;
      auto spp0 = w2kf.kf->stable_pose();
      dm::math::Rigid3d TT01 = RigidFromVector(spp0).Inverse() * RigidFromVector(sp1);
      optimizer_.AddRelativePose(w2kf.kf, kf, TT01, {info, nullptr});
    }

    // add loops
    auto loop_infos = kf->loop_infos();
    for (const auto& loop : loop_infos) {
      auto loop_kf = map_->GetKeyFrame(loop.loop_frm);
      if (!loop_kf) continue;
      VLOG(2) << "loop edge: " << loop_kf->frame_id() << "-->" << kf->frame_id() << " "
                << loop.Tlq;
      optimizer_.AddRelativePose(loop_kf, kf, loop.Tlq, {info, nullptr});
      loop_edge_cnt++;
    }
  }
  timer4.Toc();
  VLOG(2) << timer4;


  TIMER_DECORATOR(optimizer_.Solve(), Loop_RunPoseGraph_solve, 2);

  VLOG(2) << "loop edge count is: " << loop_edge_cnt;
  VLOG(2) << " ======================= loop pose graph ================\n"
            << optimizer_.summary().FullReport();
}

std::string LoopClosure::GetLoopInfo() {
  std::lock_guard<std::mutex> lg(loop_info_mutex_);
  return loop_info_;
}

cv::Mat LoopClosure::DrawMatches(KeyFrame* query_kf,
                                 KeyFrame* loop_kf,
                                 const std::vector<cv::DMatch>& matches) {
  auto query_cam = query_kf->master_cam_id();
  auto loop_cam = loop_kf->master_cam_id();
  auto query_img = query_kf->frame_meta()->images[query_cam].clone();
  auto loop_img = loop_kf->frame_meta()->images[loop_cam].clone();
  cv::cvtColor(query_img, query_img, cv::COLOR_GRAY2BGR);
  cv::cvtColor(loop_img, loop_img, cv::COLOR_GRAY2BGR);

  std::vector<cv::Point2f> loop_pts, m_loop_pts;
  auto loop_kpts = loop_kf->key_points(loop_cam);
  for (auto i = 0; i < loop_kpts.size(); i++) {
    loop_pts.emplace_back(loop_kpts[i].pt.x, loop_kpts[i].pt.y);
    auto mpt = loop_kf->GetMapPoint(loop_cam, i);
    if (mpt && !mpt->IsBad()) {
      m_loop_pts.emplace_back(loop_kpts[i].pt.x, loop_kpts[i].pt.y);
    }
  }
  for (auto pt : loop_pts) {
    cv::circle(loop_img, pt, 2, cv::Vec3b(255, 0, 0));
  }
  for (auto pt : m_loop_pts) {
    cv::circle(loop_img, pt, 2, cv::Vec3b(255, 255, 0), 2);
  }

  std::vector<cv::Point2f> query_pts, m_query_pts;
  auto query_kpts = query_kf->key_points(query_cam);
  for (auto i = 0; i < query_kpts.size(); i++) {
    query_pts.emplace_back(query_kpts[i].pt.x, query_kpts[i].pt.y);
    auto mpt = query_kf->GetMapPoint(query_cam, i);
    if (mpt && !mpt->IsBad()) {
      m_query_pts.emplace_back(query_kpts[i].pt.x, query_kpts[i].pt.y);
    }
  }
  for (auto pt : query_pts) {
    cv::circle(query_img, pt, 2, cv::Vec3b(255, 0, 0));
  }
  for (auto pt : m_query_pts) {
    cv::circle(query_img, pt, 2, cv::Vec3b(255, 255, 0), 2);
  }

  cv::Mat out_img;
  cv::hconcat(loop_img, query_img, out_img);
  int width = loop_img.cols;
  for (auto& match : matches) {
    auto pt0 = loop_pts[match.trainIdx];
    auto pt1 = query_pts[match.queryIdx];
    pt1.x += width;
    cv::line(out_img, pt0, pt1, cv::Vec3b(0, 255, 255), 1);
  }
  return out_img;
}

cv::Mat LoopClosure::DrawMapPoints(KeyFrame* kf) {
  auto loop_cam = kf->master_cam_id();
  auto loop_img = kf->frame_meta()->images[loop_cam].clone();
  cv::cvtColor(loop_img, loop_img, cv::COLOR_GRAY2BGR);

  std::vector<cv::Point2f> loop_pts, m_loop_pts;
  auto loop_kpts = kf->key_points(loop_cam);
  for (auto i = 0; i < loop_kpts.size(); i++) {
    loop_pts.emplace_back(loop_kpts[i].pt.x, loop_kpts[i].pt.y);
    auto mpt = kf->GetMapPoint(loop_cam, i);
    if (mpt && !mpt->IsBad()) {
      m_loop_pts.emplace_back(loop_kpts[i].pt.x, loop_kpts[i].pt.y);
    }
  }
  for (auto pt : loop_pts) {
    cv::circle(loop_img, pt, 2, cv::Vec3b(255, 0, 0));
  }
  for (auto pt : m_loop_pts) {
    cv::circle(loop_img, pt, 2, cv::Vec3b(255, 255, 0), 2);
  }
  return loop_img;
}

}  // namespace dm::vio::lm
