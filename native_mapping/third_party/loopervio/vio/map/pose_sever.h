// Copyright 2020 DeepMirror Inc. All rights reserved.

#ifndef VIO_MAP_POSE_SEVER_H_
#define VIO_MAP_POSE_SEVER_H_

#include <glog/logging.h>
#include <sys/stat.h>
#include <unistd.h>
#include <Eigen/Eigen>
#include <iostream>
#include <map>
#include <memory>
#include <string>
#ifdef CMAKE_BUILD
#include "../ext/rigid.h"
#else
#include "math/rigid.h"
#endif
#include "math/transformation_utils.h"

namespace dm {
namespace vio {

inline bool FileExisted(const std::string& path) { return access(path.c_str(), F_OK) != -1; }

class Vec3dNoiser {
 public:
  Vec3dNoiser() {}
  Vec3dNoiser(double mean, double stddev) : mean_(mean), stddev_(stddev) {}
  Eigen::Vector3d AddNoise(const Eigen::Vector3d& value);

 private:
  double Rand();
  double mean_ = 0.0;
  double stddev_ = 0.3;
};

// Only add noise to yaw
class YawQuatNoiser {
 public:
  YawQuatNoiser() {}
  YawQuatNoiser(double mean, double stddev) : mean_(mean), stddev_(stddev) {}
  Eigen::Quaterniond AddNoise(const Eigen::Quaterniond& value);

 private:
  double Rand();
  double mean_ = 0.0;
  double stddev_ = 0.2;
};

class FullQuatNoiser {
 public:
  FullQuatNoiser() = default;

  FullQuatNoiser(double mean, double stddev)
      : mean_(mean),
        stddev_(stddev),
        distribution_(mean, stddev),
        generator_(std::random_device {}()) {}

  Eigen::Quaterniond AddNoise(const Eigen::Quaterniond& q) {
    double rx = Rand();
    double ry = Rand();
    double rz = Rand();
    Eigen::AngleAxisd noise_x(rx, Eigen::Vector3d::UnitX());
    Eigen::AngleAxisd noise_y(ry, Eigen::Vector3d::UnitY());
    Eigen::AngleAxisd noise_z(rz, Eigen::Vector3d::UnitZ());
    Eigen::Matrix3d delta_R = (noise_z * noise_y * noise_x).toRotationMatrix();
    return Eigen::Quaterniond(delta_R) * q;
  }

 private:
  double Rand() {  return distribution_(generator_);}
  double mean_ = 0.0;
  double stddev_ = 0.2;
  std::default_random_engine generator_;
  std::normal_distribution<double> distribution_;
};

// only support tum format
class PoseServer {
 public:
  using Ptr = std::shared_ptr<PoseServer>;

  struct PoseInfo {
    Eigen::Vector3d position;
    Eigen::Quaterniond rotation;
    Eigen::Vector3d velocity;
  };

  PoseServer();
  explicit PoseServer(const std::string& gt_path, int add_noise = 0);

  bool GetPose(uint64_t stamp, Eigen::Vector3d* position, Eigen::Quaterniond* quat,
               Eigen::Vector3d* velocity = nullptr);

  bool GetPose(uint64_t stamp, dm::math::Rigid3d* pose, Eigen::Vector3d* velocity = nullptr);

  bool GetNoisedPose(uint64_t stamp, Eigen::Vector3d* position, Eigen::Quaterniond* quat,
                     Eigen::Vector3d* velocity = nullptr);

  const std::map<uint64_t, PoseInfo>& AllPoses() const { return poses_; }

 private:
  void Init();

  void WriteNoisedPose(const std::string& path);

  template <typename T>
  T interpolate(uint64_t stamp0, const T& v0, uint64_t stamp1, const T& v1, uint64_t stamp) {
    if (stamp0 == stamp1) return v0;
    CHECK_GE(stamp, stamp0);
    CHECK_GE(stamp1, stamp);
    if (stamp - stamp0 < 5e6) return v0;
    if (stamp1 - stamp < 5e6) return v1;
    return v0 + (stamp - stamp0) * (v1 - v0) / (stamp1 - stamp0);
  }

  std::string gt_path_ = "./Dataset/EuRoC/gt_tum/gt/mh01.tum";
  std::map<uint64_t, PoseInfo> poses_;
  PoseServer::Ptr noised_server_;
};

}  // namespace vio
}  // namespace dm
#endif  // VIO_MAP_POSE_SEVER_H_
