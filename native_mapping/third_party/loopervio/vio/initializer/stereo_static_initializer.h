#ifndef VIO_INITIALIZER_STEREO_STATIC_INITIALIZER_H_
#define VIO_INITIALIZER_STEREO_STATIC_INITIALIZER_H_

#include <memory>
#include <vector>
#include "vio/initializer/mono_static_initializer.h"

namespace dm::vio {

class StereoStaticInitializer : public MonoStaticInitializer {
 public:
  using Ptr = std::shared_ptr<StereoStaticInitializer>;

  StereoStaticInitializer() = default;

  StereoStaticInitializer(const std::vector<dm::common::ImageUndistortion>& camera_intrinsics,
                          const std::vector<dm::math::Rigid3d>& camera_extrinsics,
                          const InitializerConfig& config, SlidingWindowParams* params)
      : MonoStaticInitializer(camera_intrinsics, camera_extrinsics, config, params) {}
};

}  // namespace dm::vio

#endif  // VIO_INITIALIZER_STEREO_STATIC_INITIALIZER_H_
