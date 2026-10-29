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
  result["mapped"]=last!=nullptr;
  if(last) {
    loops_->Process(last);
    result["map_from_imu"]=Matrix(last->pose());
  }
  // Use the rigid component of the accepted landmarks' deformation, without rematching.
  result["wrist_updates"]=Json::array();
  for(const auto& entry:queries_) {
    const auto& query=entry.second;
    std::vector<Eigen::Vector3d> source, target;
    const auto& points=mapping_->map()->map_points();
    for(const auto& observation:query.imu_points) {
      auto found=points.find(observation.first);
      if(found==points.end() || found->second->IsBad()) continue;
      source.push_back(observation.second);
      target.push_back(found->second->WorldPoint3d());
    }
    Json updated={{"localized",source.size()>=3},{"map_revision",map_revision_}};
    if(source.size()>=3) {
      Eigen::Matrix3Xd from(3,source.size()), to(3,target.size());
      for(size_t i=0;i<source.size();++i) { from.col(i)=source[i]; to.col(i)=target[i]; }
      updated["map_from_imu"]=Matrix(dm::math::Rigid3d::FromMatrix(Eigen::umeyama(from,to,false)));
    }
    updated["camera_id"]=entry.first;
    updated["source_ns"]=query.request.at("stamp_ns");
    updated["odom_from_imu"]=query.request.at("odom_from_imu");
    updated["tracking_epoch"]=query.request.at("tracking_epoch");
    result["wrist_updates"].push_back(updated);
  }
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
  KeyFrame query(frame, std::numeric_limits<FrameId>::max(), cv::Mat());
  auto pose=loops_->Localize(&query);
  if(!pose) return {{"localized",false},{"reason","no_geometric_match"},{"map_revision",map_revision_}};
  auto evidence=loops_->evidence();
  auto previous=queries_.find(camera_id);
  if(previous!=queries_.end()) previous->second.anchor->localization_anchor_=false;
  for(auto& observation:evidence.points) observation.second=pose->Inverse()*observation.second;
  queries_[camera_id]={request,evidence.reference,std::move(evidence.points)};
  for(const auto& entry:queries_) entry.second.anchor->localization_anchor_=true;
  return {{"localized",true},{"map_from_imu",Matrix(*pose)},{"map_revision",map_revision_},
    {"matches",evidence.matches},{"inliers",evidence.inliers},
    {"grid_cells",evidence.cells},{"reprojection_rms_px",evidence.rms_px}};
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
  if(mapping_) {
    mapping_->ResetLocalMapping();
    loops_=std::make_unique<LoopClosure>(mapping_->map(),mapping_.get());
  }
  last_map_stamp_=0;
  map_revision_=0;
}
}
