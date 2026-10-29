#include "common/radial_tangential_distortion.h"

#include <vector>

namespace dm {
namespace common {

RadialTangentialDistortion::RadialTangentialDistortion() : k1_(0), k2_(0), p1_(0), p2_(0) {}

RadialTangentialDistortion::RadialTangentialDistortion(double k1, double k2, double p1, double p2)
    : k1_(k1), k2_(k2), p1_(p1), p2_(p2) {}

Eigen::Vector2d RadialTangentialDistortion::DistortPoint(const Eigen::Vector2d& y,
                                                         Eigen::Matrix2d* outJy) const {
  Eigen::Vector2d y_distortion;
  double mx2_u, my2_u, mxy_u, rho2_u, rad_dist_u;

  mx2_u = y[0] * y[0];
  my2_u = y[1] * y[1];
  mxy_u = y[0] * y[1];

  rho2_u = mx2_u + my2_u;
  rad_dist_u = k1_ * rho2_u + k2_ * rho2_u * rho2_u;

  if (outJy != nullptr) {
    Eigen::Matrix2d J;
    J.setZero();
    J(0, 0) = 1 + rad_dist_u + k1_ * 2.0 * mx2_u + k2_ * rho2_u * 4 * mx2_u + 2.0 * p1_ * y[1] +
              6 * p2_ * y[0];
    J(1, 0) = k1_ * 2.0 * y[0] * y[1] + k2_ * 4 * rho2_u * y[0] * y[1] + p1_ * 2.0 * y[0] +
              2.0 * p2_ * y[1];
    J(0, 1) = J(1, 0);
    J(1, 1) = 1 + rad_dist_u + k1_ * 2.0 * my2_u + k2_ * rho2_u * 4 * my2_u + 6 * p1_ * y[1] +
              2.0 * p2_ * y[0];
    *outJy = J;
  }

  y_distortion[0] = y[0] * (1 + rad_dist_u) + 2.0 * p1_ * mxy_u + p2_ * (rho2_u + 2.0 * mx2_u);
  y_distortion[1] = y[1] * (1 + rad_dist_u) + 2.0 * p2_ * mxy_u + p1_ * (rho2_u + 2.0 * my2_u);
  return y_distortion;
}

Eigen::Vector2d RadialTangentialDistortion::UndistortPoint(const Eigen::Vector2d& y) const {
  // using Gauss-Newton
  Eigen::Vector2d ybar = y;
  const int n = 5;
  Eigen::Matrix2d F;

  Eigen::Vector2d y_tmp;

  for (int i = 0; i < n; i++) {
    y_tmp = ybar;

    y_tmp = DistortPoint(y_tmp, &F);

    Eigen::Vector2d e(y - y_tmp);
    Eigen::Vector2d du = (F.transpose() * F).inverse() * F.transpose() * e;

    ybar += du;

    if (e.dot(e) < 1e-15) break;
  }
  return ybar;
}

std::vector<cv_mini::Point2f> RadialTangentialDistortion::DistortPoints(
    const std::vector<cv_mini::Point2f>& points) const {
  std::vector<cv_mini::Point2f> points_distorted(points.size());
  for (size_t i = 0, iend = points.size(); i < iend; ++i) {
    Eigen::Vector2d point(points[i].x, points[i].y);
    auto p = DistortPoint(point);
    points_distorted[i] = cv_mini::Point2f(p[0], p[1]);
  }
  return points_distorted;
}

std::vector<Eigen::Vector2d> RadialTangentialDistortion::UndistortPoints(
    const std::vector<Eigen::Vector2d>& points) const {
  std::vector<Eigen::Vector2d> points_undistorted(points.size());
  for (size_t i = 0, iend = points.size(); i < iend; ++i) {
    points_undistorted[i] = UndistortPoint(points[i]);
  }
  return points_undistorted;
}

void RadialTangentialDistortion::DistortParameterJacobian(const Eigen::Vector2d& imageY,
                                                          Eigen::MatrixXd* outJd) const {
  double y0 = imageY(0);
  double y1 = imageY(1);
  double r2 = y0 * y0 + y1 * y1;
  double r4 = r2 * r2;

  *outJd = Eigen::MatrixXd::Zero(2, 4);

  (*outJd)(0, 0) = y0 * r2;
  (*outJd)(0, 1) = y0 * r4;
  (*outJd)(0, 2) = 2.0 * y0 * y1;
  (*outJd)(0, 3) = r2 + 2.0 * y0 * y0;

  (*outJd)(1, 0) = y1 * r2;
  (*outJd)(1, 1) = y1 * r4;
  (*outJd)(1, 2) = r2 + 2.0 * y1 * y1;
  (*outJd)(1, 3) = 2.0 * y0 * y1;
}

std::vector<double> RadialTangentialDistortion::GetParameters() const {
  std::vector<double> params(4);
  params[0] = k1_;
  params[1] = k2_;
  params[2] = p1_;
  params[3] = p2_;
  return params;
}

void RadialTangentialDistortion::Update(const std::vector<double>& distortion) {
  k1_ = distortion[0];
  k2_ = distortion[1];
  p1_ = distortion[2];
  p2_ = distortion[3];
}

}  // namespace common
}  // namespace dm
