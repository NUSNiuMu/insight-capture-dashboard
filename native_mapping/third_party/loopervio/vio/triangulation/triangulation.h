#ifndef VIO_TRIANGULATION_TRIANGULATION_H_
#define VIO_TRIANGULATION_TRIANGULATION_H_

#include <glog/logging.h>
#include <Eigen/Eigen>
#include <optional>
#include <vector>
#ifdef CMAKE_BUILD
#include "../ext/rigid.h"
#else
#include "math/rigid.h"
#endif

namespace dm {
namespace vio {
class Triangulation {
 public:
  std::optional<Eigen::Vector3d> triangulate(const std::vector<Eigen::Vector2d>& points,
                                             const std::vector<dm::math::Rigid3d>& Twc);

  std::optional<Eigen::Vector3d> triangulateDlt(
      const std::vector<Eigen::Vector2d>& points, const std::vector<dm::math::Rigid3d>& Twc,
      bool do_chi2 = false, const std::vector<float>& chi2_th = std::vector<float>());

 private:
};

}  // namespace vio
}  // namespace dm
#endif  // VIO_TRIANGULATION_TRIANGULATION_H_
