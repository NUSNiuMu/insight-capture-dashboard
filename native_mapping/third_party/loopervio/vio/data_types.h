#ifndef VIO_DATA_TYPES_H_
#define VIO_DATA_TYPES_H_

#include <stdint.h>
#include <Eigen/Eigen>
#include <fstream>
#include <map>
#include <memory>
#include <set>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>
#include "common/image_undistortion.h"
#include "config/sensor_calibration.h"
#ifdef CMAKE_BUILD
#include "ext/mutex.h"
#include "ext/rigid.h"
#else
#include "common/base/mutex.h"
#include "math/rigid.h"
#endif
#include "math/transformation_utils.h"
#include "vio/backend/imu_preintegration.h"
#include "vio/vio_system_status.h"
#include "math/matrix_base.h"

#define TSF TimestampFormat

#define SQUARE_ROOT_INVERSE
#define FAST_COV_IN_SRIF

namespace dm {
namespace vio {

enum SensorSetType : int {
  MONOCULAR = 0,
  STEREO = 1,
  INDEPENDENT = 2,
  STEREO_PLUS_INDEPENDENT = 3,
};

struct TrackingInfo {
  VioSystemStatus tracking_status;
  size_t pt3ds_num;
  size_t slam_pts_num;
  float consuming_time;
  std::string init_info;

  int zupt_state = 0;
  float zupt_acc;
  float zupt_prl;
  std::string ToString() {
    std::stringstream str;
    str << "vio status: " << tracking_status << " "
        << (vio_status_msg.count(tracking_status) ? vio_status_msg.at(tracking_status) : "")
        << "\n";
    str << "pt3ds_num(slam_pts_num): " << pt3ds_num << "(" << slam_pts_num << ")\n";
    str << "consuming_time: " << consuming_time << "ms\n";
    if (!init_info.empty()) str << init_info << "\n";
    str << "zupt_state: " << zupt_state << " zupt_acc " << zupt_acc << " zupt_prl "
        << zupt_prl << "\n";
    return str.str();
  }
};

dm::math::Rigid3d RigidFromVector(const Eigen::Vector6d &vec);

// A <timestamp, camera_id> pair uniquely identifies an image.
using CameraId = uint32_t;
using Timestamp = uint64_t;
using FeatureId = uint32_t;
using FrameId = uint32_t;
#define INVALID_CAM_ID UINT32_MAX
using ImageId = std::pair<Timestamp, CameraId>;

// The order of state and noise in the transition matrix
enum StateOrder { O_THETA = 0, O_P = 3, O_V = 6, O_BG = 9, O_BA = 12 };
enum NoiseOrder { O_GN = 0, O_AN = 3, O_GW = 6, O_AW = 9 };

struct ImuPack {
  ImuPack() {}
  ImuPack(const std::vector<Eigen::Vector3d> &_accs, const std::vector<Eigen::Vector3d> &_gyrs,
          const std::vector<double> &_delta_ts) : accs(_accs), gyrs(_gyrs), delta_ts(_delta_ts) {}
  std::vector<Eigen::Vector3d> accs;
  std::vector<Eigen::Vector3d> gyrs;
  std::vector<double> delta_ts;
  bool IsValid(bool check_size = true) {
    if (check_size && accs.size() < 5) return false;
    if (accs.size() != gyrs.size()) return false;
    if (accs.size() != delta_ts.size() + 1) return false;
    return true;
  }
};

// The design of data structure can be seen at
// https://deepmirror.atlassian.net/wiki/spaces/VLC/pages/1557070450/MSCFK
struct BaseMeta {
 public:
  using Ptr = std::shared_ptr<BaseMeta>;

  enum DerivedType{
    FRAME = 0,
    IMU = 1,
    POINT = 2,
    LANDMARK = 3,
    CAMERA_PARAMS = 4,
  };

  BaseMeta() {}

  explicit BaseMeta(int size) : size_(size) {}

  BaseMeta(int size, int id) : size_(size), local_id_(id) {}

  virtual void Plus(const Eigen::VectorXd& dx) = 0;

  int local_id() { return local_id_; }

  void set_local_id(int new_id) { local_id_ = new_id; }

  int size() { return size_; }

  void set_size(int s) { size_ = s; }

  virtual ~BaseMeta() {}

  DerivedType derived_type;

 protected:
  // Dimension of error state
  int size_ = -1;

  // Location of error state in covariance
  int local_id_ = -1;
};

// The coordinates of the 2d feature
struct ImgCoord {
  ImgCoord() {}
  explicit ImgCoord(const Eigen::Vector2d& _uv) : uv(_uv) {}
  ImgCoord(const Eigen::Vector2d& _uv, const Eigen::Vector2d& _xy) : uv(_uv), xy(_xy) {}
  Eigen::Vector2d uv;  // in pixel
  Eigen::Vector2d xy;  // in normalized plane

  double x() const { return xy.x(); }
  double y() const { return xy.y(); }
  double u() const { return uv.x(); }
  double v() const { return uv.y(); }
};

// Information associated with a physical point. Other than the position of a point, we need
// relevant info to track whether a point is no longer needed.
struct PointMeta : public BaseMeta {
 public:
  using Ptr = std::shared_ptr<PointMeta>;
  PointMeta();
  explicit PointMeta(uint32_t feature_id);

  virtual void Plus(const Eigen::VectorXd& dx);

  void RemoveOldObservations(uint64_t timestamp, bool closed = false);

  void RemoveObservation(uint64_t timestamp);

  uint32_t feat_id;
  // map of ImageId to 2d coordinate.
  std::map<ImageId, ImgCoord> observations;

  std::map<Timestamp, std::map<CameraId, size_t>> uv_ids;

  // Except for the first sliding window, a point needs to be initialized before its position can
  // be used as a parameter block.
  bool is_initialized;
  Eigen::Vector3d position;
  double inverse_depth;

  // same to LandmarkMeta::anchor_camera_id, used when computing the observation equation
  CameraId anchor_camera_id = 0;
  // same to LandmarkMeta::anchor_timestamp, used when computing the observation equation
  Timestamp anchor_timestamp;

  bool to_delete = false;
};

// Information associated with a frame. We allow images from different cameras to have the same
// timestamp. A SlidingWindowParams "frame" is associated with a timestamp, and it can have
// multiple camera frames.
struct FrameMeta : public BaseMeta {
 public:
  using Ptr = std::shared_ptr<FrameMeta>;

  FrameMeta();

  explicit FrameMeta(Timestamp _timestamp);

  FrameMeta(Timestamp _timestamp, Timestamp raw_stamp);

  FrameMeta(const FrameMeta& other);

  virtual void Plus(const Eigen::VectorXd& dx);

  std::set<CameraId> master_cameras();

  Eigen::Vector6d RollingPose(double v, bool use_fej = true, bool with_acc = true);

  std::pair<double, Eigen::Vector6d> RollingInc(double v);

  bool IsRolling() { return !rolling_inc_poses.empty(); }

  static void set_gravity(Eigen::Vector3d _gravity) { gravity = _gravity; }

  Eigen::Quaterniond Rot() const;
  void SetRot(const Eigen::Matrix3d& rot);
  void SetRot(const Eigen::Quaterniond& quat);
  Eigen::Vector3d Trans() const;
  void SetTrans(const Eigen::Vector3d& trans);
  math::Rigid3d Pose() const;
  void SetPose(const math::Rigid3d& pose);
  Eigen::Vector6d PoseVec() const;
  void SetPoseVec(const Eigen::Vector6d& pose);

  void UpdateFEJState();

  // Timestamp in nano seconds, used as frame ID.
  Timestamp timestamp;

  // Raw timestamp, which not be optimized by VIO algorithm
  Timestamp raw_timestamp;

  // [Rwi, wPi, wVi, iBa, iBg], 'w' is the world coordinate, 'i' is the body coordinate.
  Eigen::Vector3d velocity = Eigen::Vector3d::Zero();
  Eigen::Vector3d angle_velocity = Eigen::Vector3d::Zero();

  Eigen::Vector3d accel_bias = Eigen::Vector3d::Zero();
  Eigen::Vector3d gyro_bias = Eigen::Vector3d::Zero();

  // first is row number, second is <delta_t, [delta_theta, delta_p]> for rolling, delta_t = rows *
  // line_delay
  std::map<int, std::pair<double, Eigen::Vector6d>> rolling_inc_poses;

  // X_{k|k-1}, copied from IMUMeta, and also will not be updated in EKF
  Eigen::Vector3d orientation_fej = Eigen::Vector3d::Zero();
  Eigen::Vector3d position_fej = Eigen::Vector3d::Zero();
  Eigen::Vector3d velocity_fej = Eigen::Vector3d::Zero();

  // Map of camera_id to point ids for all point observations in this frame.
  std::map<CameraId, std::vector<FeatureId>> feature_observations;
  // IMU pre_integration information
  dm::vio::ImuPreIntegration::Ptr pre_integration = nullptr;

  // Features in normalized plane coordinates
  std::map<CameraId, std::vector<Eigen::Vector2d>> xys;
  // Map of camera_id to point uvs for all point observations in this frame.
  std::map<CameraId, std::vector<Eigen::Vector2d>> uvs;

  Eigen::Vector6d last_pose;  // only used for debug

  std::map<CameraId, cv_mini::Mat> images;
  std::map<CameraId, cv::Mat> feature_pixels, feature_descriptors;

  static Eigen::Vector3d gravity;
  dm::math::Rigid3d odo_pose;

  std::map<CameraId, bool> is_master;
  std::map<CameraId, uint32_t> owner_cam_id;
  bool is_key_frame = true;

 private:
  // For pose params, the first 3 dimensions are angle axis representation of rotation, the last 3
  // dimensions are for translation.
  mutable std::shared_mutex pose_mutex_;
  Eigen::Vector6d pose_ GUARDED_BY(pose_mutex_) = Eigen::Vector6d::Zero();

  // Only those used in the thread of key frame state estimator have direct access to the pose
  friend std::ostream& operator<<(std::ostream& out, FrameMeta& data);
};

struct IMUMeta : public BaseMeta {
 public:
  using Ptr = std::shared_ptr<IMUMeta>;
  IMUMeta();

  explicit IMUMeta(uint64_t _timestamp);

  virtual void Plus(const Eigen::VectorXd& dx);

  void UpdateFEJState();

  Eigen::Quaterniond Rot() { return dm::math::AngleVectorToRotation(pose.head(3)); }

  Eigen::Vector3d Trans() { return pose.tail(3); }

  // Timestamp in nano seconds, sync with newest frame
  uint64_t timestamp;
  // For pose params, the first 3 dimensions are angle axis representation of rotation, the last 3
  // dimensions are for translation.
  // [Rwi, wPi, wVi, iBa, iBg], 'w' is the world coordinate, 'i' is the body coordinate.
  Eigen::Vector6d  pose = Eigen::Vector6d::Zero();
  Eigen::Vector3d velocity = Eigen::Vector3d::Zero();
  Eigen::Vector3d angle_velocity = Eigen::Vector3d::Zero();
  Eigen::Vector3d accel_bias = Eigen::Vector3d::Zero();
  Eigen::Vector3d gyro_bias = Eigen::Vector3d::Zero();

  // X_{k|k-1}, propagated state, will not be updated in EKF
  Eigen::Vector3d orientation_fej = Eigen::Vector3d::Zero();
  Eigen::Vector3d position_fej = Eigen::Vector3d::Zero();
  Eigen::Vector3d velocity_fej = Eigen::Vector3d::Zero();

  // IMU pre_integration information
  dm::vio::ImuPreIntegration::Ptr pre_integration = nullptr;
};

struct LandmarkMeta : public BaseMeta {
  using Ptr = std::shared_ptr<LandmarkMeta>;

  enum Representation { GLOBAL_3D, ANCHORED_MSCKF_INVERSE_DEPTH };

  LandmarkMeta();

  LandmarkMeta(uint32_t feature_id, const Eigen::Vector3d& world_position,
               const Representation& rep = GLOBAL_3D);

  LandmarkMeta(uint32_t feature_id, const Eigen::Vector3d& anchor_position, CameraId anchor_cam,
               Timestamp anchor_stamp, const Representation& rep = ANCHORED_MSCKF_INVERSE_DEPTH);

  virtual void Plus(const Eigen::VectorXd& dx);

  Eigen::Vector3d GetPosition() const;

  void SetPosition(const Eigen::Vector3d& pt, const Representation& rep);

  uint32_t feat_id;
  int fail_cnt = 0;

  // if representation is GLOBAL_3D, value is position in global coordinate
  // if representation is ANCHORED_MSCKF_INVERSE_DEPTH,
  // value[0]: projected u, value[1]: projected v, value[2]: inverse_depth
  Eigen::Vector3d value;

  // if representation is GLOBAL_3D, anchor_camera_id and anchor_timestamp are unuseful
  // if representation is ANCHORED_MSCKF_INVERSE_DEPTH, slam point is represented in camera
  // coordinate of anchor frame, anchor_camera_id is camera id of the camera coordinate
  CameraId anchor_camera_id = 0;

  // timestamp of anchor frame which slam point is represented in
  Timestamp anchor_timestamp;

  std::map<Timestamp, std::map<CameraId, size_t>> uv_ids;

  Representation representation;
};

struct CameraParamsMeta : public BaseMeta {
  using Ptr = std::shared_ptr<CameraParamsMeta>;

  enum Representation { INTRINSICS, EXTRINSICS, TIME_DELAY };

  explicit CameraParamsMeta(const Representation& rep);

  virtual void Plus(const Eigen::VectorXd& dx);

  void set_value(const Eigen::VectorXd& _value);

  // Calculate distorted coordinates for fisheye
  Eigen::Vector2d DistortPoint(const Eigen::Vector2d& ray, dm::vio::CameraModel camera_model);
  void ComputeJacobian(const Eigen::Vector2d& norm_uv, Eigen::MatrixXd* J_r_normUV,
                       Eigen::MatrixXd* J_r_intrinsics, dm::vio::CameraModel camera_model);

  Eigen::MatrixXd GetInitialCovariance();
  Eigen::MatrixXd GetInitialSqrInfo();

  // if representation is INTRINSICS, value is fx fy cx fy k2 k3 k4 k5, size
  // is 8. if representation is EXTRINSICS, value is orientation, translation,
  // size is 6 if representation is TIME_DELAY, value is delay, size is 1
  Eigen::VectorXd value;

  Representation representation;
};

class PoseWriter {
 public:
  using Ptr = std::shared_ptr<PoseWriter>;
  PoseWriter() {}

  explicit PoseWriter(std::string file);

  void WritePose(Timestamp timestamp, Eigen::Vector6d pose);
  void WritePose(Timestamp timestamp, const Eigen::Vector3d& position,
                 const Eigen::Quaterniond& quat);
  void WritePose(Timestamp timestamp, const dm::math::Rigid3d& pose);
 private:
  std::ofstream file_;
};

Eigen::Vector6d MultiplyPose(const Eigen::Vector6d& p0, const Eigen::Vector6d& dp);

Eigen::Vector6d InversePose(const Eigen::Vector6d& p);

Eigen::Vector3d TransformPoint(const Eigen::Vector6d& pose, const Eigen::Vector3d& pt);

Eigen::Vector6d InterpolationPose(const Eigen::Vector6d& pose0, Timestamp t0,
                                  const Eigen::Vector6d& pose1, Timestamp t1, Timestamp t);

Eigen::Vector6d RigidToVector(const dm::math::Rigid3d& rigid);

std::string TimestampFormat(Timestamp stamp, size_t dot = 2);

std::ostream& operator<<(std::ostream& out, FrameMeta& data);

std::ostream& operator<<(std::ostream& out, IMUMeta& data);

void WriteObj(const std::string& fileName, const std::vector<Eigen::Vector3d>& pts,
              const Eigen::Matrix4d& trans = Eigen::Matrix4d::Identity());

struct VioPointObservation {
  FeatureId point_id;
  double x;
  double y;
};

struct VioProblemFrame {
  // Timestamp is in nanoseconds.
  Timestamp timestamp = 1;
  // Raw timestamp, which not be optimized by VIO algorithm
  Timestamp raw_timestamp = 2;
  CameraId camera_id = 3;
  Eigen::Vector3d linear_acceleration;
  Eigen::Vector3d angular_velocity;
  std::vector<VioPointObservation> point_observations;
  cv_mini::Mat image = cv_mini::Mat();
  bool is_master;
  CameraId owner_cam_id = INVALID_CAM_ID;
  bool SerializeToString(std::string* output) const {
    if (!output) {
      return false;
    }
    std::ostringstream oss;
    oss.write(reinterpret_cast<const char*>(&timestamp), sizeof(Timestamp));
    oss.write(reinterpret_cast<const char*>(&raw_timestamp), sizeof(Timestamp));
    oss.write(reinterpret_cast<const char*>(&camera_id), sizeof(CameraId));
    oss.write(reinterpret_cast<const char*>(linear_acceleration.data()), 3 * sizeof(double));
    oss.write(reinterpret_cast<const char*>(angular_velocity.data()), 3 * sizeof(double));
    size_t num_point_observations = point_observations.size();
    oss.write(reinterpret_cast<const char*>(&num_point_observations), sizeof(size_t));
    for (const auto& point_observation : point_observations) {
      oss.write(reinterpret_cast<const char*>(&(point_observation.point_id)), sizeof(FeatureId));
      oss.write(reinterpret_cast<const char*>(&(point_observation.x)), sizeof(double));
      oss.write(reinterpret_cast<const char*>(&(point_observation.y)), sizeof(double));
    }
    // ignore image
    oss.write(reinterpret_cast<const char*>(&is_master), sizeof(bool));
    oss.write(reinterpret_cast<const char*>(&owner_cam_id), sizeof(CameraId));
    *output = oss.str();
    return true;
  }
  bool SerializeFromString(const std::string& input) {
    std::istringstream iss(input);
    iss.read(reinterpret_cast<char*>(&timestamp), sizeof(Timestamp));
    iss.read(reinterpret_cast<char*>(&raw_timestamp), sizeof(Timestamp));
    iss.read(reinterpret_cast<char*>(&camera_id), sizeof(CameraId));
    iss.read(reinterpret_cast<char*>(linear_acceleration.data()), 3 * sizeof(double));
    iss.read(reinterpret_cast<char*>(angular_velocity.data()), 3 * sizeof(double));
    size_t num_point_observations;
    iss.read(reinterpret_cast<char*>(&num_point_observations), sizeof(size_t));
    point_observations.resize(num_point_observations);
    for (size_t i = 0; i < num_point_observations; ++i) {
        iss.read(reinterpret_cast<char*>(&(point_observations[i].point_id)), sizeof(FeatureId));
        iss.read(reinterpret_cast<char*>(&(point_observations[i].x)), sizeof(double));
        iss.read(reinterpret_cast<char*>(&(point_observations[i].y)), sizeof(double));
    }
    // ignore image
    iss.read(reinterpret_cast<char*>(&is_master), sizeof(bool));
    iss.read(reinterpret_cast<char*>(&owner_cam_id), sizeof(CameraId));
    return true;
  }
};

struct MultiVioProblemFrame {
  std::unordered_map<CameraId, VioProblemFrame> frames;
  Timestamp timestamp() const { return frames.begin()->second.timestamp; }
  Timestamp raw_timestamp() const { return frames.begin()->second.raw_timestamp; }
  void Insert(const VioProblemFrame& frm) { frames[frm.camera_id] = frm; }
  void UpdateTimestamp(Timestamp timestamp) {
    for (auto& cam2frm : frames) {
      cam2frm.second.timestamp = timestamp;
    }
  }
  size_t size() { return frames.size(); }
  bool SerializeToString(std::string* output) const {
    if (!output) {
        return false;
    }
    std::ostringstream oss;
    // Serialize the number of frames
    size_t num_frames = frames.size();
    oss.write(reinterpret_cast<const char*>(&num_frames), sizeof(size_t));
    // Serialize each VioProblemFrame in the frames map
    for (const auto& id2frm : frames) {
        const VioProblemFrame& frm = id2frm.second;
        // Serialize the camera ID
        oss.write(reinterpret_cast<const char*>(&frm.camera_id), sizeof(CameraId));
        // Serialize the VioProblemFrame object
        std::string frame_data;
        if (!frm.SerializeToString(&frame_data)) {
            return false;
        }
        size_t length = frame_data.size();
        oss.write(reinterpret_cast<const char*>(&length), sizeof(size_t));
        oss.write(frame_data.c_str(), frame_data.size());
    }
    // Get the binary string from the stringstream
    *output = oss.str();
    return true;
  }
  bool SerializeFromString(const std::string& input) {
    std::istringstream iss(input);
    // Deserialize the number of frames
    size_t num_frames;
    iss.read(reinterpret_cast<char*>(&num_frames), sizeof(size_t));
    // Deserialize each VioProblemFrame in the frames map
    frames.clear();
    for (size_t i = 0; i < num_frames; ++i) {
        CameraId cam_id;
        iss.read(reinterpret_cast<char*>(&cam_id), sizeof(CameraId));
        // Determine the size of the serialized data for the frame
        size_t frame_data_size;
        iss.read(reinterpret_cast<char*>(&frame_data_size), sizeof(size_t));
        // Read the serialized data for the frame
        std::string frame_data(frame_data_size, ' ');
        iss.read(&frame_data[0], frame_data_size);
        VioProblemFrame frm;
        // Deserialize the frame data
        if (!frm.SerializeFromString(frame_data)) {
            return false;
        }
        frames[cam_id] = frm;
    }
    return true;
  }
};

struct FramePose {
  uint64_t timestamp = 0;
  uint64_t raw_timestamp = 0;
  Eigen::Vector3d position = Eigen::Vector3d::Zero();
  Eigen::Quaterniond orientation = Eigen::Quaterniond::Identity();
  Eigen::Vector3d acceleration_bias = Eigen::Vector3d::Zero();
  Eigen::Vector3d gyroscope_bias = Eigen::Vector3d::Zero();
  Eigen::Vector3d velocity = Eigen::Vector3d::Zero();
  Eigen::Vector3d gravity = Eigen::Vector3d::Zero();
};

struct Point3d {
  enum Type {
    MSCKF = 0,
    SLAM = 1,
  };
  Point3d() {}
  Point3d(Type _type, FeatureId _id, Timestamp _cr8_time, const Eigen::Vector3d& _pt)
      : type(_type), id(_id), cr8_time(_cr8_time), point(_pt) {}
  bool operator==(const Point3d& other) const { return id == other.id; }
  Type type;
  FeatureId id;
  Timestamp cr8_time;
  Eigen::Vector3d point;
};

struct Point3dHash {
  size_t operator()(const Point3d& obj) const { return std::hash<int>()(obj.id); }
};

std::vector<float> ComputeParallaxes(const FrameMeta::Ptr frm0, const FrameMeta::Ptr frm1,
                                     int min_feat_num = 0);

template<typename T>
std::set<T> operator+(const std::set<T> &set1, const std::set<T> &set2) {
  std::set<T> result = set1;
  result.insert(set2.begin(), set2.end());
  return result;
}

}  // namespace vio
}  // namespace dm

#endif  // VIO_DATA_TYPES_H_
