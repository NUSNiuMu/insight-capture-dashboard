#include "vio/triangulation/triangulation.h"

#include <algorithm>
#include "math/transformation_utils.h"

namespace dm {
namespace vio {

std::optional<Eigen::Vector3d> Triangulation::triangulate(
    const std::vector<Eigen::Vector2d>& points, const std::vector<dm::math::Rigid3d>& Twcs) {
  // Set the anchor to the frame which has the first observation.
  CHECK_EQ(Twcs.size(), points.size());
  // anchor_camera_pose in world, Taw = Ta(c)i * Tiw
  dm::math::Rigid3d Taw = Twcs[0].Inverse();

  // Set up the coefficients and rhs
  Eigen::Matrix3d coefficients = Eigen::Matrix3d::Zero();
  Eigen::Vector3d rhs = Eigen::Vector3d::Zero();

  for (size_t i = 0; i < points.size(); i++) {
    // camera_pose in world
    dm::math::Rigid3d Twci = Twcs[i];
    // Convert current position relative to anchor
    dm::math::Rigid3d Taci = Taw * Twci;
    Eigen::Vector3d cP(points[i].x(), points[i].y(), 1.0);

    // Get the UV coordinate normal
    Eigen::Vector3d a_cP = Taci.rotation() * cP;
    a_cP = a_cP / a_cP.norm();
    Eigen::Matrix3d Bperp = dm::math::SkewSymmetric(a_cP);
    // Append to our linear system
    Eigen::Matrix3d coef = Bperp.transpose() * Bperp;
    coefficients += coef;
    rhs += coef * Taci.translation();
  }
  // Solve the linear system
  Eigen::Vector3d P_anchor = coefficients.ldlt().solve(rhs);

  float max_base_line = 0;
  for (size_t i = 0; i < Twcs.size(); i++) {
    for (size_t j = i + 1; j < Twcs.size(); j++) {
      float dist = (Twcs[i].translation() - Twcs[j].translation()).norm();
      if (max_base_line < dist) max_base_line = dist;
    }
  }
  // Check P_anchor
  if (P_anchor[2] < 0.1 || P_anchor[2] > 100 * max_base_line || std::isnan(P_anchor.norm())) {
    return std::nullopt;
  }

  // Check coefficients
  Eigen::JacobiSVD<Eigen::Matrix3d> svd(coefficients);
  // Descending
  Eigen::Vector3d singularValues = svd.singularValues();
  double condA = singularValues(0) / singularValues(2);
  if (std::abs(condA) > 1e6) {
    return std::nullopt;
  }

  // Store it in our feature object
  Eigen::Vector3d position = Taw.Inverse() * P_anchor;

  for (size_t i = 0; i < points.size(); i++) {
    auto Tcw = Twcs[i].Inverse();
    auto cam_pt = Tcw * position;
    if (cam_pt[2] < 0) {
      return std::nullopt;
    }
  }
  // todo: check with angle
  // std::vector<double> tri_angles;
  // for (size_t i = 0; i < Twcs.size(); i++) {
  //   Eigen::Vector3d ray0 = position - Twcs[i].translation();
  //   ray0.normalize();
  //   for (size_t j = i; j < Twcs.size(); j++) {
  //     Eigen::Vector3d ray1 = position - Twcs[j].translation();
  //     ray1.normalize();
  //     double angle = 180 / M_PI * std::acos(ray0.dot(ray1));
  //     tri_angles.push_back(angle);
  //   }
  // }

  //  if(tri_angles[tri_angles.size()/2] < 2.0) return std::nullopt;

  return position;
}

std::optional<Eigen::Vector3d> Triangulation::triangulateDlt(
    const std::vector<Eigen::Vector2d>& points, const std::vector<dm::math::Rigid3d>& Twcs,
    bool do_chi2, const std::vector<float>& chi2_th) {
  Eigen::Matrix4d A = Eigen::Matrix4d::Zero();

  for (size_t i = 0; i < points.size(); i++) {
    const Eigen::Vector3d point = points[i].homogeneous().normalized();
    auto Tcw = Twcs[i].Inverse();
    const Eigen::Matrix<double, 3, 4> term =
        Tcw.Matrix34() - point * point.transpose() * Tcw.Matrix34();
    A += term.transpose() * term;
  }
  Eigen::SelfAdjointEigenSolver<Eigen::Matrix4d> eigen_solver(A);

  // Ascending
  Eigen::Vector4d singularValues = eigen_solver.eigenvalues();
  double condA = singularValues(1) / singularValues(0);
  if (std::abs(condA) < 1e3) {
    return std::nullopt;
  }

  Eigen::Vector3d pt3d = eigen_solver.eigenvectors().col(0).hnormalized();

  float max_base_line = 0;
  for (size_t i = 0; i < Twcs.size(); i++) {
    for (size_t j = i + 1; j < Twcs.size(); j++) {
      float dist = (Twcs[i].translation() - Twcs[j].translation()).norm();
      if (max_base_line < dist) max_base_line = dist;
    }
  }

  // Chi-square test
  for (size_t i = 0; i < points.size(); i++) {
    auto Tcw = Twcs[i].Inverse();
    auto cam_pt = Tcw * pt3d;
    if (cam_pt[2] < 0.1 || cam_pt[2] > 100 * max_base_line) {
      return std::nullopt;
    }
    if (!do_chi2) continue;
    float e2 =
        (Eigen::Vector2d(cam_pt[0] / cam_pt[2], cam_pt[1] / cam_pt[2]) - points[i]).squaredNorm();
    if (e2 > chi2_th[i]) return std::nullopt;
  }

  // angle
  std::vector<double> tri_angles;
  for (size_t i = 0; i < Twcs.size(); i++) {
    Eigen::Vector3d ray0 = pt3d - Twcs[i].translation();
    ray0.normalize();
    for (size_t j = i + 1; j < Twcs.size(); j++) {
      Eigen::Vector3d ray1 = pt3d - Twcs[j].translation();
      ray1.normalize();
      double angle = 180 / M_PI * std::acos(ray0.dot(ray1));
      tri_angles.push_back(angle);
    }
  }
  std::sort(tri_angles.begin(), tri_angles.end());
  double medium_angle = tri_angles[tri_angles.size() / 2];
  if (tri_angles.size() < 3 && medium_angle < 0.5) return std::nullopt;
  if (tri_angles.size() >= 3 && medium_angle < 1.0) return std::nullopt;

  return pt3d;
}
}  // namespace vio
}  // namespace dm
