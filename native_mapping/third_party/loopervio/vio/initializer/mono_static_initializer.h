#ifndef VIO_INITIALIZER_MONO_STATIC_INITIALIZER_H_
#define VIO_INITIALIZER_MONO_STATIC_INITIALIZER_H_

#include <memory>
#include <vector>
#include <deque>
#include "vio/initializer/base_initializer.h"

namespace dm::vio {

class MonoStaticInitializer : public BaseInitializer {
 public:
  using Ptr = std::shared_ptr<MonoStaticInitializer>;

  MonoStaticInitializer() {}

  MonoStaticInitializer(const std::vector<dm::common::ImageUndistortion>& camera_intrinsics,
                        const std::vector<dm::math::Rigid3d>& camera_extrinsics,
                        const InitializerConfig& config, SlidingWindowParams* params)
      : BaseInitializer(camera_intrinsics, camera_extrinsics, config, params) {}

  bool Solve();

 protected:
  std::deque<FrameMeta::Ptr> frames_;

  size_t max_sw_size_ = 7;

  virtual bool IsStatic();

  Timestamp first_stamp_ = 0;
};

}  // namespace dm::vio

#endif  // VIO_INITIALIZER_MONO_STATIC_INITIALIZER_H_
