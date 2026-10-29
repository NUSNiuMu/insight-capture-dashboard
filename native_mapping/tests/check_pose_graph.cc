#include <iostream>
#include "vio/map/ba_optimizer.h"
#include "vio/map/key_frame.h"
#include "vio/map/map_point.h"
#include "vio/map/map.h"

int main() {
  using namespace dm::vio;
  using namespace dm::vio::lm;
  dm::common::ImageUndistortion intrinsic;
  intrinsic.Init(544,640,{306.,306.,272.,320.},{0.,0.,0.,0.},CameraModel::PINHOLE);
  Eigen::Matrix4d first=Eigen::Matrix4d::Identity();
  first.block<3,3>(0,0)=Eigen::AngleAxisd(.4,Eigen::Vector3d(1,2,3).normalized()).toRotationMatrix();
  first.block<3,1>(0,3)=Eigen::Vector3d(2,-1,.5);
  Eigen::Matrix4d step=Eigen::Matrix4d::Identity();
  step(0,3)=.2; step(1,3)=.1;
  auto a=dm::math::Rigid3d::FromMatrix(first);
  auto relative=dm::math::Rigid3d::FromMatrix(step);
  std::vector<dm::common::ImageUndistortion> intrinsics={intrinsic};
  std::vector<dm::math::Rigid3d> extrinsics={dm::math::Rigid3d::FromMatrix(Eigen::Matrix4d::Identity())};
  KeyFrame::InitCameraParameters(intrinsics,extrinsics);
  auto frame=[](uint64_t stamp,const dm::math::Rigid3d& pose) {
    auto value=std::make_shared<FrameMeta>(stamp,stamp);
    value->odo_pose=pose; value->SetPose(pose);
    value->images[0]=cv::Mat::zeros(640,544,CV_8U);
    value->feature_pixels[0]=(cv::Mat_<float>(1,2)<<272.f,320.f);
    value->feature_descriptors[0]=cv::Mat::ones(1,256,CV_32F)/16.f;
    value->is_master={{0,true}}; value->owner_cam_id={{0,0}};
    return value;
  };
  KeyFrame anchor(frame(1'000'000'000,a));
  KeyFrame next(frame(1'500'000'000,a*relative));
  // A nonidentity world origin exposes an uninitialized fixed anchor.
  BAOptimizerTS graph(intrinsics,extrinsics,.2,30);
  graph.AddRelativePose(&anchor,&next,relative,{Eigen::Matrix6d::Identity(),nullptr});
  graph.Solve();
  const auto& result=graph.key_frames();
  double error=(RigidFromVector(result.at(anchor.frame_id())).Matrix44()-first).norm();
  error+=(RigidFromVector(result.at(next.frame_id())).Matrix44()-first*step).norm();
  if(!std::isfinite(error) || error>1e-8) {
    std::cerr << "pose graph anchor error: " << error << '\n'; return 1;
  }
  MapPoint observed(a*Eigen::Vector3d(0,0,2),&anchor);
  MapPoint compatible(a*Eigen::Vector3d(.001,0,2),&anchor);
  MapPoint incompatible(a*Eigen::Vector3d(.3,0,2),&anchor);
  observed.AddObservation(&anchor,0,0);
  if(observed.PredictScale(1.2f,2.f)!=0) {
    std::cerr << "SuperPoint was assigned an ORB pyramid level\n"; return 1;
  }
  if(!observed.CheckAllObsValidateIn(&compatible) ||
      observed.CheckAllObsValidateIn(&incompatible)) {
    std::cerr << "landmark merge ignored geometric observations\n"; return 1;
  }
  Map map;
  auto retained=new KeyFrame(frame(2'000'000'000,a));
  map.AddKeyFrame(retained);
  retained->localization_anchor_=true;
  if(map.EraseKeyFrameTS(retained)) return 1;
  map.TryClearKeyFrames();
  if(map.KeyFramesSize()!=1) { std::cerr << "wrist anchor was culled\n"; return 1; }
  retained->localization_anchor_=false;
  if(!map.EraseKeyFrameTS(retained) || map.KeyFramesSize()!=0) return 1;
  auto pending=new KeyFrame(frame(2'500'000'000,a));
  map.AddKeyFrame(pending);
  pending->forced_retention_=true;
  if(map.EraseKeyFrameTS(pending)) return 1;
  pending->localization_anchor_=true;
  map.TryClearKeyFrames();
  if(map.KeyFramesSize()!=1) { std::cerr << "queued wrist anchor was freed\n"; return 1; }
  pending->localization_anchor_=false;
  map.TryClearKeyFrames();
  if(map.KeyFramesSize()!=0) return 1;
  std::cout << "pose graph, geometric merge and wrist anchor lifetime: passed\n";
}
