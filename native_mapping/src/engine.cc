#include "engine.h"
#include <chrono>
#include <stdexcept>
#include <limits>
#include <Eigen/Geometry>
#include "vio/data_types.h"
#include "vio/map/orb_matcher.h"
namespace insight {
using namespace dm::vio;
using namespace dm::vio::lm;
namespace {
dm::math::Rigid3d Transform(const Json& input) {
  Eigen::Matrix4d matrix;
  for (int r=0;r<4;r++) for(int c=0;c<4;c++) matrix(r,c)=input.at(r).at(c);
  return dm::math::Rigid3d::FromMatrix(matrix);
}
Json Matrix(const dm::math::Rigid3d& pose) {
  Eigen::Matrix4d value = pose.Matrix44(); Json result=Json::array();
  for(int r=0;r<4;r++) { Json row=Json::array(); for(int c=0;c<4;c++) row.push_back(value(r,c)); result.push_back(row); }
  return result;
}
}
Json Engine::Initialize(const Json& config) {
  queries_.clear();
  pending_queries_.clear();
  last_map_stamp_=0;
  map_revision_=0;
  std::vector<dm::common::ImageUndistortion> intrinsics;
  std::vector<dm::math::Rigid3d> extrinsics;
  for (const auto& camera : config.at("cameras")) {
    dm::common::ImageUndistortion intrinsic;
    intrinsic.Init(camera.at("width"),camera.at("height"),camera.at("intrinsics").get<std::vector<double>>(),std::vector<double>(4,0),dm::vio::CameraModel::PINHOLE);
    intrinsics.push_back(intrinsic); extrinsics.push_back(Transform(camera.at("imu_from_camera")));
  }
  KeyFrame::InitCameraParameters(intrinsics,extrinsics);
  mapping_=std::make_unique<LocalMapping>();
  loops_=std::make_unique<LoopClosure>(mapping_->map(),mapping_.get());
  return Stats();
}
Json Engine::Map(const Json& request, const std::vector<cv::Mat>& images) {
  if (!mapping_ || images.size()!=5) throw std::runtime_error("mapping expects stereo images and matched features");
  pending_queries_.clear();
  uint64_t stamp=request.at("stamp_ns");
  if (stamp<=last_map_stamp_) throw std::runtime_error("unordered mapping frame");
  auto frame=std::make_shared<FrameMeta>(stamp,stamp);
  frame->odo_pose=Transform(request.at("odom_from_imu")); frame->SetPose(frame->odo_pose);
  frame->images[0]=images[0]; frame->images[1]=images[1];
  frame->feature_pixels={{0,images[2]},{1,images[3]}};
  frame->feature_descriptors={{0,images[4]},{1,images[4]}};
  frame->is_master={{0,true},{1,false}}; frame->owner_cam_id={{0,0},{1,0}};
  mapping_->Runt(frame); last_map_stamp_=stamp;
  ++map_revision_;
  auto result=Stats(); auto last=mapping_->map()->LastKeyFrame();
  result["mapped"]=last && last->timestamp()==stamp;
  result["verified_relocalization"]=false;
  if(result["mapped"].get<bool>()) {
    const bool loop=loops_->Process(last);
    result["verified_relocalization"]=loop || mapping_->pose_inliers()>=40;
    result["map_from_imu"]=Matrix(last->pose());
  }
  // Refit the accepted image correspondences against the optimized map, without rematching.
  result["wrist_updates"]=Json::array();
  for(auto& entry:queries_) {
    auto& query=entry.second;
    std::vector<MapPoint*> matched(query.frame->FeatureSize(entry.first),nullptr);
    size_t retained=0;
    std::set<FeatureId> identities;
    for(auto& observation:query.observations) {
      auto point=mapping_->map()->ResolveMapPoint(observation.second);
      if(point) observation.second=point->point_id();
      if(!point || point->IsBad() || point->NumOfObs()<3 || !point->state().refined ||
         !identities.insert(point->point_id()).second) continue;
      matched[observation.first]=point;
      ++retained;
    }
    auto seed=query.anchor->pose()*query.anchor_pose.Inverse()*query.frame->pose();
    auto pose=loops_->RefinePose(query.frame.get(),matched,seed,10,.8);
    auto evidence=loops_->evidence();
    Json updated={{"localized",pose.has_value()},{"map_revision",map_revision_},
      {"reason",evidence.reason},{"alignment",{{"retained",retained},
        {"original",query.observations.size()},{"inliers",evidence.inliers},
        {"reprojection_rms_px",evidence.rms_px},{"grid_cells",evidence.cells},
        {"condition_ratio",evidence.condition_ratio}}}};
    if(pose) {
      updated["map_from_imu"]=Matrix(*pose);
      query.frame->set_pose(*pose);
      query.anchor_pose=query.anchor->pose();
    }
    updated["camera_id"]=entry.first;
    updated["source_ns"]=query.request.at("stamp_ns");
    updated["odom_from_imu"]=query.request.at("odom_from_imu");
    updated["tracking_epoch"]=query.request.at("tracking_epoch");
    result["wrist_updates"].push_back(updated);
  }
  mapping_->map()->ClearPointReplacements();
  return result;
}
Json Engine::Localize(const Json& request, const std::vector<cv::Mat>& arrays) {
  if(arrays.size()!=3) throw std::runtime_error("localization expects image, pixels and descriptors");
  const auto& image=arrays[0];
  if (!mapping_) throw std::runtime_error("mapping is not initialized");
  if (Stats().at("landmarks").get<int>()<40)
    return {{"localized",false},{"reason","map_not_ready"},{"map_revision",map_revision_}};
  int camera_id=request.at("camera_id");
  if(camera_id<2 || camera_id>3) throw std::runtime_error("invalid wrist camera id");
  auto frame=std::make_shared<FrameMeta>(request.at("stamp_ns").get<uint64_t>(), request.at("stamp_ns").get<uint64_t>());
  frame->odo_pose=Transform(request.at("odom_from_imu")); frame->SetPose(frame->odo_pose);
  frame->images[camera_id]=image; frame->is_master={{camera_id,true}};
  frame->feature_pixels={{camera_id,arrays[1]}};
  frame->feature_descriptors={{camera_id,arrays[2]}};
  frame->owner_cam_id={{camera_id,camera_id}};
  auto query=std::make_unique<KeyFrame>(frame, std::numeric_limits<FrameId>::max(), cv::Mat());
  auto pose=loops_->Localize(query.get());
  if(!pose) return {{"localized",false},{"reason","no_geometric_match"},{"map_revision",map_revision_}};
  auto evidence=loops_->evidence();
  query->set_pose(*pose);
  pending_queries_[camera_id]={request,evidence.reference,evidence.reference->pose(),std::move(query),
                              std::move(evidence.observations)};
  return {{"localized",true},{"map_from_imu",Matrix(*pose)},{"map_revision",map_revision_},
    {"verified_relocalization",evidence.verified_references>=2},
    {"matches",evidence.matches},{"inliers",evidence.inliers},
    {"grid_cells",evidence.cells},{"reprojection_rms_px",evidence.rms_px}};
}
Json Engine::Accept(const Json& request) {
  const int camera_id=request.at("camera_id");
  auto pending=pending_queries_.find(camera_id);
  if(pending==pending_queries_.end() ||
     pending->second.request.at("stamp_ns")!=request.at("stamp_ns") ||
     pending->second.request.at("tracking_epoch")!=request.at("tracking_epoch"))
    throw std::runtime_error("accepted localization does not match the pending observation");
  auto previous=queries_.find(camera_id);
  if(previous!=queries_.end()) previous->second.anchor->localization_anchor_=false;
  queries_.insert_or_assign(camera_id,std::move(pending->second));
  pending_queries_.erase(pending);
  for(const auto& entry:queries_) entry.second.anchor->localization_anchor_=true;
  return {{"accepted",true}};
}
Json Engine::Stats() const {
  if (!mapping_) return {{"keyframes",0},{"landmarks",0},{"map_revision",map_revision_}};
  size_t valid=0;
  for(const auto& entry:mapping_->map()->map_points()) {
    auto point=entry.second;
    valid+=!point->IsBad() && point->NumOfObs()>=3 && point->state().refined;
  }
  return {{"keyframes",mapping_->map()->KeyFramesSize()},{"landmarks",valid},{"map_revision",map_revision_},
          {"raw_landmarks",mapping_->map()->MapPointsSize()}};
}
std::vector<cv::Mat> Engine::Geometry() const {
  cv::Mat features(0,259,CV_32F), reference(0,5,CV_32F);
  if(!mapping_) return {features,reference};
  for(const auto& entry:mapping_->map()->map_points()) {
    auto point=entry.second;
    if(point->IsBad() || point->NumOfObs()<3 || !point->state().refined) continue;
    cv::Mat row(1,259,CV_32F);
    for(int j=0;j<3;++j) row.at<float>(0,j)=point->WorldPoint3d()[j];
    point->GetDescriptor().copyTo(row.colRange(3,259));
    features.push_back(row);
  }
  auto last=mapping_->map()->LastKeyFrame();
  if(last) {
    const auto& points=last->map_points(0);
    const auto& pixels=last->key_points(0);
    for(size_t i=0;i<points.size();++i) {
      auto point=points[i];
      if(!point || point->IsBad() || point->NumOfObs()<3 || !point->state().refined) continue;
      cv::Mat row(1,5,CV_32F);
      for(int j=0;j<3;++j) row.at<float>(0,j)=point->WorldPoint3d()[j];
      row.at<float>(0,3)=pixels[i].pt.x; row.at<float>(0,4)=pixels[i].pt.y;
      reference.push_back(row);
    }
  }
  return {features,reference};
}
void Engine::Reset() {
  queries_.clear();
  pending_queries_.clear();
  if(mapping_) {
    mapping_->ResetLocalMapping();
    loops_=std::make_unique<LoopClosure>(mapping_->map(),mapping_.get());
  }
  last_map_stamp_=0;
  map_revision_=0;
}
}
