#ifndef VIO_INITIALIZER_MONO_DYNAMIC_INITIALIZER_H_
#define VIO_INITIALIZER_MONO_DYNAMIC_INITIALIZER_H_

#include <memory>
#include <vector>
#include <map>
#include <unordered_map>
#include "ceres/ceres.h"
#include "common/image_undistortion.h"
#include "vio/initializer/base_initializer.h"

#define INITIALIZATION_DEBUG_MODE_ 0

namespace dm {
namespace vio {
class MonoDynamicInitializer : public BaseInitializer {
 public:
  using Ptr = std::shared_ptr<MonoDynamicInitializer>;

  enum State {
    TO_SOLVE_INIT_TOW_FRAMES,
    TO_SOLVE_MORE_FRAMES,
    TO_ALIGN_WITH_IMU,
    INIT_SUCCESS,
  };

  MonoDynamicInitializer() {}

  MonoDynamicInitializer(const std::vector<dm::common::ImageUndistortion>& camera_intrinsics,
                         const std::vector<dm::math::Rigid3d>& camera_extrinsics,
                         const InitializerConfig& config, SlidingWindowParams* params)
      : BaseInitializer(camera_intrinsics, camera_extrinsics, config, params) {}

  bool Solve();

 private:
  bool SolveRelativePose(const FrameMeta::Ptr frm0, const FrameMeta::Ptr frm1);

  bool SolveRelativePoseRolling(const FrameMeta::Ptr frm0, const FrameMeta::Ptr frm1);

  bool InitFromTwoFrames();

  bool SolveNewFrame();

  void ClearSlidingWindow();

  bool IsNewKeyFrame();

  bool SolvePoseByPnp(FrameMeta::Ptr frm);

  bool SolvePoseByPnpRolling(FrameMeta::Ptr frm);

  void Optimize();

  void OptimizeRolling();

  void Filter(float max_error);

  void PreProcess();

  bool InitializePoint(FeatureId feat_id, bool is_rolling);

  void FullInertialBA();

  bool CheckVelocity();

  void InitVelocity();

  bool has_pre_processed = false;

  bool pop_last_frame_ = false;
  State state_ = TO_SOLVE_INIT_TOW_FRAMES;
  std::map<ImageId, cv_mini::Mat> images_;

  std::unordered_map<FeatureId, int> optimized_cnts_;

  size_t sliding_window_size_ = 9;

  Timestamp start_stamp_ = 0;

  int try_cnt_ = 0;
};
}  // namespace vio
}  // namespace dm

#endif  // VIO_INITIALIZER_MONO_DYNAMIC_INITIALIZER_H_
