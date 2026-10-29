// Copyright 2020 DeepMirror Inc. All rights reserved.
#include "vio/map/pose_sever.h"

#include <cmath>
#include <fstream>
#include <iomanip>
#include <random>
#include <string>
#include "glog/logging.h"
#include "vio/data_types.h"

namespace dm {
namespace vio {

double Vec3dNoiser::Rand() {
  std::random_device rd;
  std::default_random_engine engine(rd());
  std::normal_distribution<double> n(mean_, stddev_);
  return n(engine);
}

Eigen::Vector3d Vec3dNoiser::AddNoise(const Eigen::Vector3d& v) {
  auto p0 = Rand();
  auto p1 = Rand();
  auto p2 = Rand();
  Eigen::Vector3d p(p0, p1, p2);
  VLOG(2) << "position noise: " << p.transpose() << " | " << p.norm();
  return v + p;
}

double YawQuatNoiser::Rand() {
  std::random_device rd;
  std::default_random_engine engine(rd());
  std::normal_distribution<double> n(M_PI * mean_ / 180.0, M_PI * stddev_ / 180.0);
  return n(engine);
}

Eigen::Quaterniond YawQuatNoiser::AddNoise(const Eigen::Quaterniond& q) {
  auto r = Rand();
  Eigen::Matrix3d delta_R = Eigen::AngleAxisd(r, Eigen::Vector3d::UnitZ()).toRotationMatrix();
  VLOG(2) << "YawQuatNoiser: " << r;
  return Eigen::Quaterniond(delta_R) * q;
}

PoseServer::PoseServer() { Init(); }
PoseServer::PoseServer(const std::string& gt_path, int add_noise) : gt_path_(gt_path) {
  Init();
  if (add_noise == -1) return;
  if (add_noise == 1 || !FileExisted(gt_path_ + ".noise")) {
    WriteNoisedPose(gt_path_ + ".noise");
  }
  noised_server_ = std::make_shared<PoseServer>(gt_path_ + ".noise", -1);
}

void PoseServer::Init() {
  if (gt_path_.empty()) return;
  std::ifstream file(gt_path_);
  poses_.clear();
  std::string line;
  while (std::getline(file, line)) {
    std::replace(line.begin(), line.end(), ',', ' ');
    std::stringstream ss;
    ss << line;
    double stamp;
    ss >> stamp;
    stamp *= 1e9;
    PoseInfo info;
    for (int i = 0; i < 3; i++) {
      ss >> info.position[i];
    }
    ss >> info.rotation.x();
    ss >> info.rotation.y();
    ss >> info.rotation.z();
    ss >> info.rotation.w();
    poses_[uint64_t(stamp)] = info;
  }

  return;
}

void PoseServer::WriteNoisedPose(const std::string& path) {
  PoseWriter writer(path);
  Vec3dNoiser vec_noiser;
  YawQuatNoiser quat_noiser;
  for (auto& stamp2gt : poses_) {
    Eigen::Vector3d p = vec_noiser.AddNoise(stamp2gt.second.position);
    Eigen::Quaterniond q = quat_noiser.AddNoise(stamp2gt.second.rotation);
    writer.WritePose(stamp2gt.first, p, q);
  }
}

template <>
Eigen::Quaterniond PoseServer::interpolate<Eigen::Quaterniond>(uint64_t stamp0,
                                                               const Eigen::Quaterniond& v0,
                                                               uint64_t stamp1,
                                                               const Eigen::Quaterniond& v1,
                                                               uint64_t stamp) {
  if (stamp0 == stamp1) return v0;
  CHECK_GE(stamp, stamp0);
  CHECK_GE(stamp1, stamp);
  if (stamp - stamp0 < 5e6) return v0;
  if (stamp1 - stamp < 5e6) return v1;
  double t = (stamp - stamp0) / static_cast<double>(stamp1 - stamp0);
  return v0.slerp(t, v1);
}

bool PoseServer::GetPose(uint64_t stamp, Eigen::Vector3d* position, Eigen::Quaterniond* quat,
                         Eigen::Vector3d* velocity) {
  auto back_iter = poses_.upper_bound(stamp);
  if (back_iter == poses_.end()) return false;

  CHECK_GE(back_iter->first, stamp);
  auto before_iter = back_iter;
  before_iter--;
  if (position)
    *position = interpolate(before_iter->first, before_iter->second.position, back_iter->first,
                            back_iter->second.position, stamp);

  if (quat)
    *quat = interpolate(before_iter->first, before_iter->second.rotation, back_iter->first,
                        back_iter->second.rotation, stamp);

  if (velocity)
    *velocity = interpolate(before_iter->first, before_iter->second.velocity, back_iter->first,
                            back_iter->second.velocity, stamp);
  return true;
}

bool PoseServer::GetPose(uint64_t stamp, dm::math::Rigid3d* pose, Eigen::Vector3d* velocity) {
  Eigen::Vector3d p;
  Eigen::Quaterniond q;
  bool state = GetPose(stamp, &p, &q, velocity);
  if (!state) return false;
  *pose = dm::math::Rigid3d(p, q);
  return true;
}

bool PoseServer::GetNoisedPose(uint64_t stamp, Eigen::Vector3d* position, Eigen::Quaterniond* quat,
                               Eigen::Vector3d* velocity) {
  if (!noised_server_) return false;
  return noised_server_->GetPose(stamp, position, quat, velocity);
}

}  // namespace vio
}  // namespace dm
