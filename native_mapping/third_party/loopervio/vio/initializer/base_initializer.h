#ifndef VIO_INITIALIZER_BASE_INITIALIZER_H_
#define VIO_INITIALIZER_BASE_INITIALIZER_H_

#include <vector>
#include <string>
#include <memory>

#include "vio/backend/backend_config.h"
#include "vio/backend/sliding_window_params.h"

namespace dm::vio {

class BaseInitializer {
 public:
  using Ptr = std::shared_ptr<BaseInitializer>;

  BaseInitializer() = default;

  BaseInitializer(const std::vector<dm::common::ImageUndistortion>& camera_intrinsics,
                  const std::vector<dm::math::Rigid3d>& camera_extrinsics,
                  const InitializerConfig& config, SlidingWindowParams* params)
      : camera_intrinsics_(camera_intrinsics),
        camera_extrinsics_(camera_extrinsics),
        params_(params),
        config_(config) {}

  virtual ~BaseInitializer() = default;

  virtual bool Solve() = 0;

  const std::string& Info() { return info_; }

  const dm::common::ImageUndistortion &camera_intrinsic(CameraId camera_id) const {
    return camera_intrinsics_[camera_id];
  }

  const dm::math::Rigid3d &camera_extrinsic(CameraId camera_id) const {
    return camera_extrinsics_[camera_id];
  }

 protected:
  std::vector<dm::common::ImageUndistortion> camera_intrinsics_;
  // Pose of camera in IMU coordinate.
  std::vector<dm::math::Rigid3d> camera_extrinsics_;
  SlidingWindowParams *params_ = nullptr;
  InitializerConfig config_;
  std::string info_;
};
}  // namespace dm::vio
#endif  // VIO_INITIALIZER_BASE_INITIALIZER_H_
