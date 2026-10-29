#ifndef VIO_INITIALIZER_STEREO_DYNAMIC_INITIALIZER_H_
#define VIO_INITIALIZER_STEREO_DYNAMIC_INITIALIZER_H_
#include <memory>
#include <vector>
#include <map>
#include <unordered_map>
#include "ceres/ceres.h"
#include "common/image_undistortion.h"
#include "vio/initializer/base_initializer.h"

namespace dm::vio {

class StereoDynamicInitializer : public BaseInitializer {
 public:
  enum State {
    TO_SOLVE_INIT_TOW_FRAMES,
    TO_SOLVE_MORE_FRAMES,
    TO_ALIGN_WITH_IMU,
    INIT_SUCCESS,
  };

  StereoDynamicInitializer() {}

  StereoDynamicInitializer(const std::vector<dm::common::ImageUndistortion>& camera_intrinsics,
                           const std::vector<dm::math::Rigid3d>& camera_extrinsics,
                           const InitializerConfig& config, SlidingWindowParams* params)
      : BaseInitializer(camera_intrinsics, camera_extrinsics, config, params) {}

  bool Solve();

 private:
  size_t StereoFrameTriangulate(FrameMeta::Ptr frm);

  size_t FrameTriangulate(FrameMeta::Ptr frm);

  bool SolvePoseByPnp(FrameMeta::Ptr frm, size_t min_inlier_cnt = 15);

  bool SlidingWindowBundleAdjustment();

  bool StereoVisionOdometry();

  bool IsNewKeyFrame();

  State state_ = TO_SOLVE_MORE_FRAMES;

  bool pop_last_frame_ = false;

  size_t sw_size_ = 7;

  double scale_ = 0.0;

  size_t try_num_ = 0;

  size_t init_pts_ = 0;

  Timestamp first_stamp_ = 0;
};
}  // namespace dm::vio
#endif  // VIO_INITIALIZER_STEREO_DYNAMIC_INITIALIZER_H_
