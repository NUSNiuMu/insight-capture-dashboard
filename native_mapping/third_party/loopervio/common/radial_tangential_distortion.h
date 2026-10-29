#ifndef COMMON_RADIAL_TANGENTIAL_DISTORTION_H_
#define COMMON_RADIAL_TANGENTIAL_DISTORTION_H_

#include <memory>
#include <vector>
#include "common/distortion.h"

namespace dm {
namespace common {
class RadialTangentialDistortion : public Distortion {
 private:
  /// \brief the first radial distortion parameter
  double k1_;
  /// \brief the second radial distortion parameter
  double k2_;
  /// \brief the first tangential distortion parameter
  double p1_;
  /// \brief the second tangential distortion parameter
  double p2_;

 public:
  using Ptr = std::shared_ptr<RadialTangentialDistortion>;
  RadialTangentialDistortion();
  RadialTangentialDistortion(double k1, double k2, double p1, double p2);

  Eigen::Vector2d DistortPoint(const Eigen::Vector2d& y,
                               Eigen::Matrix2d* outJy = nullptr) const override;
  Eigen::Vector2d UndistortPoint(const Eigen::Vector2d& y) const override;
  std::vector<cv_mini::Point2f> DistortPoints(
      const std::vector<cv_mini::Point2f>& points) const override;
  std::vector<Eigen::Vector2d> UndistortPoints(
      const std::vector<Eigen::Vector2d>& points) const override;

  void DistortParameterJacobian(const Eigen::Vector2d& y, Eigen::MatrixXd* outJy) const override;
  std::vector<double> GetParameters() const override;
  void Update(const std::vector<double>& distortion) override;
};
}  // namespace common
}  // namespace dm

#endif  // COMMON_RADIAL_TANGENTIAL_DISTORTION_H_
