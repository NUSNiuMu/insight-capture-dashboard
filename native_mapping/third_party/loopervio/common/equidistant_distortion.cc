#include "common/equidistant_distortion.h"

#include <algorithm>
#include <vector>

namespace dm {
namespace common {
EquidistantDistortion::EquidistantDistortion() : k1_(0), k2_(0), k3_(0), k4_(0) {}
EquidistantDistortion::EquidistantDistortion(double k1, double k2, double k3, double k4)
    : k1_(k1), k2_(k2), k3_(k3), k4_(k4) {}

Eigen::Vector2d EquidistantDistortion::DistortPoint(const Eigen::Vector2d& norm_uv,
                                                    Eigen::Matrix2d* J_r_normUV) const {
  // Calculate distorted coordinates for fisheye
  double r = norm_uv.norm();
  double theta = std::atan(r);
  double theta_d = theta + k1_ * std::pow(theta, 3) + k2_ * std::pow(theta, 5) +
                   k3_ * std::pow(theta, 7) + k4_ * std::pow(theta, 9);

  // Handle when r is small (meaning our xy is near the camera center)
  double inv_r = (r > 1e-8) ? 1.0 / r : 1.0;
  double cdist = (r > 1e-8) ? theta_d * inv_r : 1.0;

  if (J_r_normUV != nullptr) {
    // Jacobian of "normalized" pixel to normalized pixel
    Eigen::Matrix<double, 2, 2> dxy_dxyn = Eigen::Matrix<double, 2, 2>::Zero();
    dxy_dxyn << cdist, 0, 0, cdist;

    // Jacobian of "normalized" pixel to r
    Eigen::Matrix<double, 2, 1> dxy_dr = Eigen::Matrix<double, 2, 1>::Zero();
    dxy_dr << -norm_uv(0) * cdist * inv_r, -norm_uv(1) * cdist * inv_r;

    // Jacobian of r pixel to normalized xy
    Eigen::Matrix<double, 1, 2> dr_dxyn = Eigen::Matrix<double, 1, 2>::Zero();
    dr_dxyn << norm_uv(0) * inv_r, norm_uv(1) * inv_r;

    // Jacobian of "normalized" pixel to theta_d
    Eigen::Matrix<double, 2, 1> dxy_dthd = Eigen::Matrix<double, 2, 1>::Zero();
    dxy_dthd << norm_uv(0) * inv_r, norm_uv(1) * inv_r;

    // Jacobian of theta_d to theta
    double dthd_dth = 1 + 3 * k1_ * std::pow(theta, 2) + 5 * k2_ * std::pow(theta, 4) +
                      7 * k3_ * std::pow(theta, 6) + 9 * k4_ * std::pow(theta, 8);

    // Jacobian of theta to r
    double dth_dr = 1 / (r * r + 1);

    // Total Jacobian wrt normalized pixel coordinates
    *J_r_normUV = dxy_dxyn + (dxy_dr + dxy_dthd * dthd_dth * dth_dr) * dr_dxyn;
  }

  // Calculate distorted coordinates for fisheye
  double x1 = norm_uv(0) * cdist;
  double y1 = norm_uv(1) * cdist;
  return Eigen::Vector2d(x1, y1);
}

Eigen::Vector2d EquidistantDistortion::UndistortPoint(const Eigen::Vector2d& point) const {
  const double mx = point.x();
  const double my = point.y();
  double thetad = sqrt(mx * mx + my * my);
  double theta(0);
  double scaling(1);
  if (thetad > 1e-5) {
    theta = solveTheta(thetad);
    scaling = sin(theta) / thetad;
  }
  cv_mini::Point2f ray(mx * scaling / cos(theta), my * scaling / cos(theta));
  if (ray.x < -1e5 && ray.y < -1e5) {
    Eigen::Vector2d p(point.x(), point.y());
    double theta, phi;
    backprojectSymmetric(p, &theta, &phi);
    ray.x = sin(theta) * cos(phi) / cos(theta);
    ray.y = sin(theta) * sin(phi) / cos(theta);
  }
  return Eigen::Vector2d(ray.x, ray.y);
}

std::vector<cv_mini::Point2f> EquidistantDistortion::DistortPoints(
    const std::vector<cv_mini::Point2f>& points) const {
  std::vector<cv_mini::Point2f> points_distorted(points.size());
  for (size_t i = 0, iend = points.size(); i < iend; ++i) {
    Eigen::Vector2d point(points[i].x, points[i].y);
    auto p = DistortPoint(point);
    points_distorted[i] = cv_mini::Point2f(p[0], p[1]);
  }
  return points_distorted;
}

void EquidistantDistortion::backprojectSymmetric(const Eigen::Vector2d& p_u, double* theta,
                                                 double* phi) const {
  double tol = 1e-10;
  double p_u_norm = p_u.norm();

  if (p_u_norm < 1e-10) {
    *phi = 0.0;
  } else {
    *phi = atan2(p_u(1), p_u(0));
  }

  int npow = 9;

  Eigen::MatrixXd coeffs(npow + 1, 1);
  coeffs.setZero();
  coeffs(0) = -p_u_norm;
  coeffs(1) = 1.0;

  coeffs(3) = k1_;
  coeffs(5) = k2_;
  coeffs(7) = k3_;
  coeffs(9) = k4_;

  // Get eigenvalues of companion matrix corresponding to polynomial.
  // Eigenvalues correspond to roots of polynomial.
  Eigen::MatrixXd A(npow, npow);
  A.setZero();
  A.block(1, 0, npow - 1, npow - 1).setIdentity();
  A.col(npow - 1) = -coeffs.block(0, 0, npow, 1) / coeffs(npow);

  Eigen::EigenSolver<Eigen::MatrixXd> es(A);
  Eigen::MatrixXcd eigval = es.eigenvalues();

  std::vector<double> thetas;
  for (int i = 0; i < eigval.rows(); ++i) {
    if (fabs(eigval(i).imag()) > tol) {
      continue;
    }

    double t = eigval(i).real();

    if (t < -tol) {
      continue;
    } else if (t < 0.0) {
      t = 0.0;
    }

    thetas.push_back(t);
  }

  if (thetas.empty()) {
    *theta = p_u_norm;
  } else {
    *theta = *std::min_element(thetas.begin(), thetas.end());
  }
}

double EquidistantDistortion::solveTheta(double r_theta) const {
  double d_func_d_theta;
  double theta = r_theta;

  for (int i = 3; i > 0; i--) {
    double theta2 = theta * theta;
    double func = k4_ * theta2;
    func += k3_;
    func *= theta2;
    func += k2_;
    func *= theta2;
    func += k1_;
    func *= theta2;
    func += 1;
    func *= theta;

    d_func_d_theta = 9 * k4_ * theta2;
    d_func_d_theta += 7 * k3_;
    d_func_d_theta *= theta2;
    d_func_d_theta += 5 * k2_;
    d_func_d_theta *= theta2;
    d_func_d_theta += 3 * k1_;
    d_func_d_theta *= theta2;
    d_func_d_theta += 1;

    // Iteration of Newton method
    theta += (r_theta - func) / d_func_d_theta;
  }

  return theta;
}


std::vector<Eigen::Vector2d> EquidistantDistortion::UndistortPoints(
    const std::vector<Eigen::Vector2d>& points) const {
  std::vector<Eigen::Vector2d> points_undistorted(points.size());
  for (size_t i = 0, iend = points.size(); i < iend; ++i) {
    points_undistorted[i] = UndistortPoint(points[i]);
  }
  return points_undistorted;
}

void EquidistantDistortion::DistortParameterJacobian(const Eigen::Vector2d& norm_uv,
                                                     Eigen::MatrixXd* outJy) const {
  *outJy = Eigen::MatrixXd::Zero(2, 4);
  double r = norm_uv.norm();
  double theta = std::atan(r);
  double inv_r = (r > 1e-8) ? 1.0 / r : 1.0;

  (*outJy)(0, 0) = norm_uv(0) * inv_r * std::pow(theta, 3);
  (*outJy)(0, 1) = norm_uv(0) * inv_r * std::pow(theta, 5);
  (*outJy)(0, 2) = norm_uv(0) * inv_r * std::pow(theta, 7);
  (*outJy)(0, 3) = norm_uv(0) * inv_r * std::pow(theta, 9);

  (*outJy)(1, 0) = norm_uv(1) * inv_r * std::pow(theta, 3);
  (*outJy)(1, 1) = norm_uv(1) * inv_r * std::pow(theta, 5);
  (*outJy)(1, 2) = norm_uv(1) * inv_r * std::pow(theta, 7);
  (*outJy)(1, 3) = norm_uv(1) * inv_r * std::pow(theta, 9);
}


std::vector<double> EquidistantDistortion::GetParameters() const {
  std::vector<double> params(4);
  params[0] = k1_;
  params[1] = k2_;
  params[2] = k3_;
  params[3] = k4_;
  return params;
}

void EquidistantDistortion::Update(const std::vector<double>& distortion) {
  k1_ = distortion[0];
  k2_ = distortion[1];
  k3_ = distortion[2];
  k4_ = distortion[3];
}

}  // namespace common
}  // namespace dm
