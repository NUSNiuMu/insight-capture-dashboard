#ifndef VIO_POSE_INTERPOLATION_H_
#define VIO_POSE_INTERPOLATION_H_

#include <vector>
#include <utility>
#ifdef CMAKE_BUILD
#include "ext/rigid.h"
#else
#include "math/rigid.h"
#endif

namespace dm {
namespace vio {

class PoseInterpolation {
 public:
  static std::vector<double> Interpolation(const std::vector<double>& pose1,
                                           const std::vector<double>& pose2,
                                           const double& dst_timestamp);
  static dm::math::Rigid3d Interpolation(std::pair<uint64_t, dm::math::Rigid3d> pose1,
                                         std::pair<uint64_t, dm::math::Rigid3d> pose2,
                                         uint64_t dst_timestamp);
  static std::vector<double> SE3Interpolation(const std::vector<double>& pose1,
                                              const std::vector<double>& pose2,
                                              const double& dst_timestamp);
};
}  // namespace vio
}  // namespace dm
#endif  // VIO_POSE_INTERPOLATION_H_
