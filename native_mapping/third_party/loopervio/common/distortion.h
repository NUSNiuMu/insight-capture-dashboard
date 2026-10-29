#ifndef COMMON_DISTORTION_H_
#define COMMON_DISTORTION_H_

#include <Eigen/Dense>
#include <memory>
#include <vector>
#ifdef __EMSCRIPTEN__
#include "cv_mini/cv_mini.hpp"
#else
#include "opencv2/opencv.hpp"
#define cv_mini cv
#endif

namespace dm {
namespace common {
class Distortion {
 public:
  using Ptr = std::shared_ptr<Distortion>;
  Distortion() {}
  virtual ~Distortion() {}

  virtual std::vector<cv_mini::Point2f> DistortPoints(
      const std::vector<cv_mini::Point2f>& points) const = 0;

  /**
   * @brief Apply distortion to a point in the normalized image plane
   *
   * @param y The point in the normalized image plane
   * @param outJy The Jacobian of the distortion function with respect to small
   * changes in the input point
   * @return point is distorted
   */
  virtual Eigen::Vector2d DistortPoint(const Eigen::Vector2d& y,
                                       Eigen::Matrix2d* outJy = nullptr) const = 0;

  /**
   * @brief Apply undistortion to recover a point in the normalized image plane
   *
   * @param y The distorted point
   * @return point is in the normalized image plane
   */
  virtual Eigen::Vector2d UndistortPoint(const Eigen::Vector2d& point) const = 0;
  virtual std::vector<Eigen::Vector2d> UndistortPoints(
      const std::vector<Eigen::Vector2d>& points) const = 0;

  /**
   * \brief Apply distortion to the point and provide the Jacobian of the
   * distortion with respect to small changes in the distortion parameters
   *
   * @param y the point in the normalized image plane.
   * @param outJy  the Jacobian of the distortion with respect to small changes
   * in the distortion parameters.
   */
  virtual void DistortParameterJacobian(const Eigen::Vector2d& y, Eigen::MatrixXd* outJy) const = 0;
  virtual std::vector<double> GetParameters() const = 0;
  virtual void Update(const std::vector<double>& distortion) = 0;
};

}  // namespace common
}  // namespace dm

#endif  // COMMON_DISTORTION_H_
