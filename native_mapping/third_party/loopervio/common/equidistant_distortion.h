#ifndef COMMON_EQUIDISTANT_DISTORTION_H_
#define COMMON_EQUIDISTANT_DISTORTION_H_

#include <memory>
#include <vector>
#include "common/distortion.h"

namespace dm {
namespace common {
class EquidistantDistortion : public Distortion {
 private:
  double k1_, k2_, k3_, k4_;
  void backprojectSymmetric(const Eigen::Vector2d& p_u, double* theta, double* phi) const;
  double solveTheta(double r_theta) const;

 public:
  using Ptr = std::shared_ptr<EquidistantDistortion>;
  EquidistantDistortion();
  EquidistantDistortion(double k1, double k2, double k3, double k4);

  Eigen::Vector2d DistortPoint(const Eigen::Vector2d& y,
                               Eigen::Matrix2d* outJy = nullptr) const override;
  Eigen::Vector2d UndistortPoint(const Eigen::Vector2d& point) const override;
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

#endif  // COMMON_EQUIDISTANT_DISTORTION_H_
