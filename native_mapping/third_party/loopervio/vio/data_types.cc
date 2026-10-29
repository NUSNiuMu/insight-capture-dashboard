#include "vio/data_types.h"

#include <algorithm>
#include <iomanip>

#include "common/distortion.h"
#include "common/equidistant_distortion.h"
#include "common/radial_tangential_distortion.h"

namespace dm {
namespace vio {

PointMeta::PointMeta()
    : BaseMeta(3), is_initialized(false), position{0.0, 0.0, 0.0}, inverse_depth{-1} {
  derived_type = POINT;
}

PointMeta::PointMeta(uint32_t feature_id)
    : BaseMeta(3),
      feat_id(feature_id),
      is_initialized(false),
      position{0.0, 0.0, 0.0},
      inverse_depth{-1} {
  derived_type = POINT;
}

void PointMeta::Plus(const Eigen::VectorXd& dx) {
  CHECK(dx.size() == size());
  position += dx;
}

void PointMeta::RemoveOldObservations(uint64_t timestamp, bool closed) {
  auto cmp_func = closed ? [](Timestamp a, Timestamp b) { return a <= b; }
                         : [](Timestamp a, Timestamp b) { return a < b; };
  for (auto it = uv_ids.begin(); it != uv_ids.end();) {
    if (cmp_func(it->first, timestamp)) {
      it = uv_ids.erase(it);
    } else {
      break;
    }
  }
  for (auto it = observations.begin(); it != observations.end();) {
    if (cmp_func(it->first.first, timestamp)) {
      it = observations.erase(it);
    } else {
      it++;
    }
  }
}

void PointMeta::RemoveObservation(uint64_t timestamp) {
  for (auto it = uv_ids.begin(); it != uv_ids.end();) {
    if (it->first == timestamp) {
      it = uv_ids.erase(it);
    } else {
      it++;
    }
  }
  for (auto it = observations.begin(); it != observations.end();) {
    if (it->first.first == timestamp) {
      it = observations.erase(it);
    } else {
      it++;
    }
  }
}

Eigen::Vector3d FrameMeta::gravity = Eigen::Vector3d(0, 0, -9.8107);

FrameMeta::FrameMeta() : BaseMeta(6), timestamp(0) {
  derived_type = FRAME;
}

FrameMeta::FrameMeta(Timestamp _timestamp) : BaseMeta(6), timestamp(_timestamp) {
  derived_type = FRAME;
}

FrameMeta::FrameMeta(Timestamp _timestamp, Timestamp raw_stamp)
    : BaseMeta(6), timestamp(_timestamp), raw_timestamp(raw_stamp) {
  derived_type = FRAME;
}

FrameMeta::FrameMeta(const FrameMeta &other) {
  timestamp = other.timestamp;
  raw_timestamp = other.raw_timestamp;
  velocity = other.velocity;
  accel_bias = other.accel_bias;
  gyro_bias = other.gyro_bias;
  rolling_inc_poses = other.rolling_inc_poses;
  orientation_fej = other.orientation_fej;
  position_fej = other.position_fej;
  velocity_fej = other.velocity_fej;
  feature_observations = other.feature_observations;
  derived_type = FRAME;
  if (other.pre_integration)
    pre_integration =
        std::make_shared<ImuPreIntegration>(*other.pre_integration);
  xys = other.xys;
  uvs = other.uvs;
  last_pose = other.last_pose;
  images = other.images;
  is_master = other.is_master;
  owner_cam_id = other.owner_cam_id;
  std::shared_lock<std::shared_mutex> lg(other.pose_mutex_);
  pose_ = other.pose_;
}


void FrameMeta::Plus(const Eigen::VectorXd& dx) {
  std::unique_lock<std::shared_mutex> lock(pose_mutex_);
  CHECK(dx.size() == size());
  Eigen::Quaterniond raw_q = dm::math::AngleVectorToRotation(pose_.head(3));
  Eigen::Quaterniond dx_q = dm::math::AngleVectorToRotation(dx.block<3, 1>(0, 0));
  pose_.head(3) = dm::math::AngleVectorFromRotation(raw_q * dx_q);
  pose_.tail(3) += dx.block<3, 1>(3, 0);
}

std::set<CameraId> FrameMeta::master_cameras() {
  std::set<CameraId> ret;
  for (auto& id2m : is_master) {
    if (id2m.second) ret.insert(id2m.first);
  }
  return ret;
}

Eigen::Vector6d FrameMeta::RollingPose(double v, bool use_fej, bool with_acc) {
  std::unique_lock<std::shared_mutex> lock(pose_mutex_);
  int round_v = round(v);
  Eigen::Vector6d out_pose = pose_;
  if (!IsRolling()) return out_pose;
  CHECK(rolling_inc_poses.count(round_v)) << " RollingPose error: " << v << " " << round_v;

  double delta_t = rolling_inc_poses.at(round_v).first;
  Eigen::Vector6d delta_pose = rolling_inc_poses.at(round_v).second;

  const auto& vel = use_fej ? velocity_fej : velocity;
  out_pose.tail(3) = pose_.tail(3) + vel * delta_t;
  if (with_acc) {
    out_pose.tail(3) += 0.5 * gravity * delta_t * delta_t +
                        dm::math::AngleVectorToRotation(pose_.head(3)) * delta_pose.tail(3);
  }
  out_pose.head(3) =
      dm::math::AngleVectorFromRotation(dm::math::AngleVectorToRotation(pose_.head(3)) *
                                        dm::math::AngleVectorToRotation(delta_pose.head(3)));

  return out_pose;
}

std::pair<double, Eigen::Vector6d> FrameMeta::RollingInc(double v) {
  CHECK(rolling_inc_poses.count(round(v))) << " RollingInc error: " << v;
  return rolling_inc_poses.at(round(v));
}

Eigen::Quaterniond FrameMeta::Rot() const {
  std::shared_lock<std::shared_mutex> lock(pose_mutex_);
  return dm::math::AngleVectorToRotation(pose_.head(3));
}

void FrameMeta::SetRot(const Eigen::Matrix3d& rot) {
  std::shared_lock<std::shared_mutex> lock(pose_mutex_);
  pose_.head(3) = dm::math::AngleVectorFromRotation(rot);
}

void FrameMeta::SetRot(const Eigen::Quaterniond& quat) {
  std::shared_lock<std::shared_mutex> lock(pose_mutex_);
  pose_.head(3) = dm::math::AngleVectorFromRotation(quat.toRotationMatrix());
}

Eigen::Vector3d FrameMeta::Trans() const {
  std::shared_lock<std::shared_mutex> lock(pose_mutex_);
  return pose_.tail(3);
}

void FrameMeta::SetTrans(const Eigen::Vector3d& trans) {
  std::shared_lock<std::shared_mutex> lock(pose_mutex_);
  pose_.tail(3) = trans;
}

math::Rigid3d FrameMeta::Pose() const {
  std::shared_lock<std::shared_mutex> lock(pose_mutex_);
  return RigidFromVector(pose_);
}

void FrameMeta::SetPose(const math::Rigid3d& pose) {
  std::shared_lock<std::shared_mutex> lock(pose_mutex_);
  pose_ = RigidToVector(pose);
}

Eigen::Vector6d FrameMeta::PoseVec() const {
  std::shared_lock<std::shared_mutex> lock(pose_mutex_);
  return pose_;
}

void FrameMeta::SetPoseVec(const Eigen::Vector6d& pose) {
  std::shared_lock<std::shared_mutex> lock(pose_mutex_);
  pose_ = pose;
}

void FrameMeta::UpdateFEJState() {
  std::shared_lock<std::shared_mutex> lock(pose_mutex_);
  orientation_fej = pose_.head(3);
  position_fej = pose_.tail(3);
  velocity_fej = velocity;
}


#ifdef SQUARE_ROOT_INVERSE
IMUMeta::IMUMeta() : BaseMeta(9), timestamp(0) {}
IMUMeta::IMUMeta(uint64_t _timestamp) : BaseMeta(9), timestamp(_timestamp) {}
#else
IMUMeta::IMUMeta() : BaseMeta(15), timestamp(0) {}
IMUMeta::IMUMeta(uint64_t _timestamp) : BaseMeta(15), timestamp(_timestamp) {}
#endif

void IMUMeta::Plus(const Eigen::VectorXd& dx) {
  CHECK(dx.size() == size());
#ifdef SQUARE_ROOT_INVERSE
  velocity += dx.block<3, 1>(O_V - 6, 0);
  accel_bias += dx.block<3, 1>(O_BA - 6, 0);
  gyro_bias += dx.block<3, 1>(O_BG - 6, 0);
#else
  Eigen::Quaterniond raw_q = dm::math::AngleVectorToRotation(pose.head(3));
  Eigen::Quaterniond dx_q = dm::math::AngleVectorToRotation(dx.block<3, 1>(O_THETA, 0));
  pose.head(3) = dm::math::AngleVectorFromRotation(raw_q * dx_q);
  pose.tail(3) += dx.block<3, 1>(O_P, 0);
  velocity += dx.block<3, 1>(O_V, 0);
  accel_bias += dx.block<3, 1>(O_BA, 0);
  gyro_bias += dx.block<3, 1>(O_BG, 0);
#endif
}

void IMUMeta::UpdateFEJState() {
  orientation_fej = pose.head(3);
  position_fej = pose.tail(3);
  velocity_fej = velocity;
}

LandmarkMeta::LandmarkMeta() {
  derived_type = LANDMARK;
}

LandmarkMeta::LandmarkMeta(uint32_t feature_id, const Eigen::Vector3d& world_position,
                           const LandmarkMeta::Representation& rep)
    : BaseMeta(3), feat_id(feature_id), representation(rep) {
  SetPosition(world_position, rep);
  derived_type = LANDMARK;
}

LandmarkMeta::LandmarkMeta(uint32_t feature_id, const Eigen::Vector3d& anchor_position,
                           CameraId anchor_cam, Timestamp anchor_stamp,
                           const LandmarkMeta::Representation& rep)
    : BaseMeta(3),
      feat_id(feature_id),
      anchor_camera_id(anchor_cam),
      anchor_timestamp(anchor_stamp),
      representation(rep) {
  SetPosition(anchor_position, rep);
  derived_type = LANDMARK;
}

void LandmarkMeta::Plus(const Eigen::VectorXd& dx) {
  value += dx;
}

Eigen::Vector3d LandmarkMeta::GetPosition() const {
  switch (representation) {
    case GLOBAL_3D:
      return value;
    case ANCHORED_MSCKF_INVERSE_DEPTH:
      double inv_d = value[2];
      Eigen::Vector3d p(0, 0, 1 / inv_d);
      p[0] = value[0] / inv_d;
      p[1] = value[1] / inv_d;
      return p;
  }
  return Eigen::Vector3d(0.0, 0.0, 0.0);
}

void LandmarkMeta::SetPosition(const Eigen::Vector3d& pt, const Representation& rep) {
  representation = rep;
  switch (representation) {
    case GLOBAL_3D:
      value = pt;
      break;
    case ANCHORED_MSCKF_INVERSE_DEPTH:
      value[0] = pt[0] / pt[2];
      value[1] = pt[1] / pt[2];
      value[2] = 1.0 / pt[2];
      break;
  }
}

CameraParamsMeta::CameraParamsMeta(const Representation& rep) : representation(rep) {
  switch (representation) {
    case INTRINSICS:
      set_size(8);
      break;
    case EXTRINSICS:
      set_size(6);
      break;
    case TIME_DELAY:
      set_size(1);
      break;
    default:
      CHECK(0) << "Online calibration, camera params type is not support now.";
  }
}

void CameraParamsMeta::Plus(const Eigen::VectorXd& dx) {
  CHECK(dx.size() == size());
  if (representation == EXTRINSICS) {
    Eigen::Quaterniond raw_q = dm::math::AngleVectorToRotation(value.head(3));
    Eigen::Quaterniond dx_q = dm::math::AngleVectorToRotation(dx.block<3, 1>(0, 0));
    value.head(3) = dm::math::AngleVectorFromRotation(raw_q * dx_q);
    value.tail(3) += dx.block<3, 1>(3, 0);
  } else if (representation == INTRINSICS || representation == TIME_DELAY) {
    value += dx;
  } else {
    CHECK(0) << "Online calibration, camera params type is not support now.";
  }
}

void CameraParamsMeta::set_value(const Eigen::VectorXd& _value) { value = _value; }

Eigen::Vector2d CameraParamsMeta::DistortPoint(const Eigen::Vector2d& ray,
                                               dm::vio::CameraModel camera_model) {
  Eigen::Vector2d norm_dist;
  switch (camera_model) {
    case dm::vio::CameraModel::PINHOLE:
      norm_dist = std::make_shared<dm::common::RadialTangentialDistortion>(value(4), value(5),
                                                                           value(6), value(7))
                      ->DistortPoint(ray);
      break;
    case dm::vio::CameraModel::FISHEYE:
      norm_dist = std::make_shared<dm::common::EquidistantDistortion>(value(4), value(5), value(6),
                                                                      value(7))
                      ->DistortPoint(ray);
      break;
    default:
      CHECK(0) << "invalidate camera model";
      break;
  }

  return Eigen::Vector2d(value(0) * norm_dist(0) + value(2), value(1) * norm_dist(1) + value(3));
}

void CameraParamsMeta::ComputeJacobian(const Eigen::Vector2d& norm_uv, Eigen::MatrixXd* J_r_normUV,
                                       Eigen::MatrixXd* J_r_intrinsics,
                                       dm::vio::CameraModel camera_model) {
  dm::common::Distortion::Ptr distortion_model = nullptr;
  switch (camera_model) {
    case dm::vio::CameraModel::PINHOLE:
      distortion_model = std::make_shared<dm::common::RadialTangentialDistortion>(
          value(4), value(5), value(6), value(7));
      break;
    case dm::vio::CameraModel::FISHEYE:
      distortion_model = std::make_shared<dm::common::EquidistantDistortion>(value(4), value(5),
                                                                             value(6), value(7));
      break;
    default:
      CHECK(0) << "invalidate camera model";
      break;
  }

  // Jacobian of distorted pixel to "normalized" pixel
  Eigen::Matrix<double, 2, 2> duv_dxy = Eigen::Matrix<double, 2, 2>::Zero();
  duv_dxy << value(0), 0, 0, value(1);

  Eigen::Matrix2d J_D_normUV;
  Eigen::Vector2d uv_distorted = distortion_model->DistortPoint(norm_uv, &J_D_normUV);

  // Total Jacobian wrt normalized pixel coordinates
  *J_r_normUV = Eigen::MatrixXd::Zero(2, 2);
  *J_r_normUV = duv_dxy * J_D_normUV;

  Eigen::MatrixXd J_D_params;
  distortion_model->DistortParameterJacobian(norm_uv, &J_D_params);

  *J_r_intrinsics = Eigen::MatrixXd::Zero(2, 8);
  (*J_r_intrinsics)(0, 0) = uv_distorted(0);
  (*J_r_intrinsics)(0, 2) = 1;
  (*J_r_intrinsics)(1, 1) = uv_distorted(1);
  (*J_r_intrinsics)(1, 3) = 1;
  (*J_r_intrinsics).block<2, 4>(0, 4) = duv_dxy * J_D_params;
}

Eigen::MatrixXd CameraParamsMeta::GetInitialCovariance() {
  Eigen::MatrixXd init_cov = Eigen::MatrixXd::Identity(size_, size_);
  switch (representation) {
    case INTRINSICS:
      init_cov.block<0, 0>(4, 4).diagonal() *= std::pow(1e-2, 2);
      init_cov.block<4, 4>(4, 4).diagonal() *= std::pow(1e-4, 2);
      break;
    case EXTRINSICS:
      init_cov.block<3, 3>(0, 0).diagonal() *= std::pow(0.001, 2);
      init_cov.block<3, 3>(3, 3).diagonal() *= std::pow(0.01, 2);
      break;
    case TIME_DELAY:
      init_cov(0, 0) = std::pow(1e-2, 2);
      break;
    default:
      CHECK(0) << "Online calibration, camera params type is not support now.";
  }
  return init_cov;
}

Eigen::MatrixXd CameraParamsMeta::GetInitialSqrInfo() {
  Eigen::MatrixXd init_sqrt_info = Eigen::MatrixXd::Identity(size_, size_);
  switch (representation) {
    case INTRINSICS:
      init_sqrt_info.block<4, 4>(4, 4).diagonal() *= 2000.0;  // for distortion parameters
      break;
    case EXTRINSICS:
      init_sqrt_info.block<3, 3>(0, 0).diagonal() *= 1000.0;
      init_sqrt_info.block<3, 3>(3, 3).diagonal() *= 100.0;
      break;
    case TIME_DELAY:
      init_sqrt_info(0, 0) *= 100.0;
      break;
    default:
      CHECK(0) << "Online calibration, camera params type is not support now.";
  }
  return init_sqrt_info;
}

PoseWriter::PoseWriter(std::string file) {
  file_.open(file);
  CHECK(file_.is_open());
}

void PoseWriter::WritePose(Timestamp timestamp, Eigen::Vector6d pose) {
  if (timestamp == 0) {
    LOG(WARNING) << "timestamp is invalid";
    return;
  }
  Eigen::Quaterniond q = dm::math::AngleVectorToRotation(pose.head(3));
  file_ << std::setprecision(20) << timestamp * 1e-9 << " " << pose[3] << " " << pose[4] << " "
        << pose[5] << " " << q.x() << " " << q.y() << " " << q.z() << " " << q.w() << std::endl;
}

void PoseWriter::WritePose(Timestamp timestamp, const Eigen::Vector3d& position,
                           const Eigen::Quaterniond& quat) {
  file_ << std::setprecision(20) << timestamp * 1e-9 << " " << position[0] << " " << position[1]
        << " " << position[2] << " " << quat.x() << " " << quat.y() << " " << quat.z() << " "
        << quat.w() << std::endl;
}

void PoseWriter::WritePose(Timestamp timestamp, const dm::math::Rigid3d& pose) {
  auto position = pose.translation();
  auto quat = pose.rotation();
  file_ << std::setprecision(20) << timestamp * 1e-9 << " " << position[0] << " " << position[1]
        << " " << position[2] << " " << quat.x() << " " << quat.y() << " " << quat.z() << " "
        << quat.w() << std::endl;
}

Eigen::Vector6d MultiplyPose(const Eigen::Vector6d& p0, const Eigen::Vector6d& dp) {
  Eigen::Quaterniond q0 = dm::math::AngleVectorToRotation(p0.head(3));
  Eigen::Quaterniond dq = dm::math::AngleVectorToRotation(dp.head(3));
  Eigen::Vector6d ret;
  ret.head(3) = dm::math::AngleVectorFromRotation(q0 * dq);
  ret.tail(3) = q0.toRotationMatrix() * dp.tail(3) + p0.tail(3);
  return ret;
}

Eigen::Vector6d InversePose(const Eigen::Vector6d& p) {
  Eigen::Quaterniond q = dm::math::AngleVectorToRotation(p.head(3));
  Eigen::Quaterniond inv_q = q.conjugate();
  Eigen::Vector6d ret;
  ret.head(3) = -p.head(3);
  ret.tail(3) = -(inv_q * p.tail(3));
  return ret;
}

Eigen::Vector3d TransformPoint(const Eigen::Vector6d& pose, const Eigen::Vector3d& pt) {
  Eigen::Quaterniond q = dm::math::AngleVectorToRotation(pose.head(3));
  return q * pt + pose.tail(3);
}

Eigen::Vector6d InterpolationPose(const Eigen::Vector6d& pose0, Timestamp t0,
                                  const Eigen::Vector6d& pose1, Timestamp t1, Timestamp t) {
  Eigen::Vector3d trans = pose0.tail(3) + (pose1.tail(3) - pose0.tail(3)) *
                                              static_cast<double>(t - t0) /
                                              static_cast<double>(t1 - t0);
  Eigen::Quaterniond q0 = dm::math::AngleVectorToRotation(pose0.head(3));
  Eigen::Quaterniond q1 = dm::math::AngleVectorToRotation(pose1.head(3));
  Eigen::Quaterniond q = q0.slerp(static_cast<double>(t - t0) / static_cast<double>(t1 - t0), q1);
  Eigen::Vector6d ret;
  ret.head(3) = dm::math::AngleVectorFromRotation(q);
  ret.tail(3) = trans;
  return ret;
}

dm::math::Rigid3d RigidFromVector(const Eigen::Vector6d& vec) {
  Eigen::Quaterniond q = dm::math::AngleVectorToRotation(vec.head(3));
  dm::math::Rigid3d rigid(vec.tail(3), q);
  return rigid;
}

Eigen::Vector6d RigidToVector(const dm::math::Rigid3d& rigid) {
  Eigen::Vector6d vec;
  vec.head(3) = dm::math::AngleVectorFromRotation(rigid.rotation());
  vec.tail(3) = rigid.translation();
  return vec;
}

std::string TimestampFormat(Timestamp stamp, size_t dot) {
  std::stringstream ss;
  ss << std::fixed << std::setprecision(dot) << stamp * 1e-9;
  return ss.str();
}

std::ostream& operator<<(std::ostream& out, FrameMeta& data) {
  out << "timestamp: " << TimestampFormat(data.timestamp) << ", ";
  out << "position: " << data.Trans().transpose() << ", ";
  out << "orientation: " << data.Rot().coeffs().transpose() << ", ";
  out << "velocity: " << data.velocity.transpose() << ", ";
  out << "accel_bias: " << data.accel_bias.transpose() << ", ";
  out << "gyro_bias: " << data.gyro_bias.transpose();
  return out;
}

std::ostream& operator<<(std::ostream& out, IMUMeta& data) {
  out << "timestamp: " << TimestampFormat(data.timestamp) << ", ";
  out << "pose: " << data.pose.transpose() << ", ";
  out << "velocity: " << data.velocity.transpose() << ", ";
  out << "accel_bias: " << data.accel_bias.transpose() << ", ";
  out << "gyro_bias: " << data.gyro_bias.transpose();
  return out;
}

void WriteObj(const std::string& fileName, const std::vector<Eigen::Vector3d>& pts,
              const Eigen::Matrix4d& trans) {
  const Eigen::Matrix3d& R = trans.block<3, 3>(0, 0);
  const Eigen::Vector3d& T = trans.block<3, 1>(0, 3);
  std::ofstream file(fileName);
  for (size_t i = 0; i < pts.size(); ++i) {
    const Eigen::Vector3d p = R * pts[i] + T;
    file << "v " << p.transpose() << std::endl;
  }
  file.close();
}

std::vector<float> ComputeParallaxes(const FrameMeta::Ptr frm0, const FrameMeta::Ptr frm1,
                                     int min_feat_num) {
  std::set<uint32_t> master_cam_ids = frm1->master_cameras();
  const uint32_t master_cam_id = master_cam_ids.size() > 0 ? *master_cam_ids.begin() : 0;

  const size_t feat_num = frm0->feature_observations[master_cam_id].size();
  if (feat_num < min_feat_num) return std::vector<float>();

  std::vector<float> parallaxes;
  for (size_t i = 0; i < frm0->feature_observations[master_cam_id].size(); i++) {
    auto feat_id = frm0->feature_observations[master_cam_id][i];
    for (size_t j = 0; j < frm1->feature_observations[master_cam_id].size(); j++) {
      const auto& feat_jd = frm1->feature_observations[master_cam_id][j];
      if (feat_id == feat_jd) {
        float parallax = (frm0->uvs[master_cam_id][i] - frm1->uvs[master_cam_id][j]).norm();
        parallaxes.push_back(parallax);
        break;
      }
    }
  }
  std::sort(parallaxes.begin(), parallaxes.end());
  return parallaxes;
}

}  // namespace vio
}  // namespace dm
