#ifndef VIO_RECON_PLANE_CORE_H_
#define VIO_RECON_PLANE_CORE_H_
#include <vector>
#include "Eigen/Eigen"
#include "ceres/ceres.h"
#include "math/transformation_utils.h"
namespace dm::recon {

template<typename Point>
class PlaneEstimator {
 public:
  PlaneEstimator() = default;
  virtual ~PlaneEstimator() = default;
  using X_t = Point;
  using Y_t = double;
  using M_t = Eigen::Quaterniond;
  static const int kMinNumSamples = 3;
  virtual std::vector<M_t> Estimate(const std::vector<X_t>& A,
                                    const std::vector<Y_t>& B) {
    Eigen::MatrixXd A_mat(A.size(), 3);
    Eigen::VectorXd b_vec(A.size());
    for (int i = 0; i < A.size(); i++) {
      A_mat.row(i) << A[i].x(), A[i].y(), A[i].z();
      b_vec(i) = -1.0;
    }
    Eigen::Vector3d planeParams = A_mat.colPivHouseholderQr().solve(b_vec);
    Eigen::Quaterniond result;
    result.vec() = planeParams;
    result.w() = 1.0;
    result.normalize();
    return {result};
  }

  virtual void Residuals(const std::vector<X_t>& A, const std::vector<Y_t>& B,
                         const M_t& X, std::vector<double>* residuals) {
    double norm = X.vec().norm();
    residuals->resize(A.size());
    for (int i = 0; i < A.size(); i++) {
      double residual = abs(X.x() * A[i].x() + X.y() * A[i].y() + X.z() * A[i].z() + X.w());
      residual /= norm;
      (*residuals)[i] = residual;
    }
  }
};

template<typename Point>
class HorizontalPlaneEstimator : public PlaneEstimator<Point> {
 public:
  HorizontalPlaneEstimator() = default;
  ~HorizontalPlaneEstimator() override = default;
  using typename PlaneEstimator<Point>::X_t;
  using typename PlaneEstimator<Point>::Y_t;
  using typename PlaneEstimator<Point>::M_t;

  static const int kMinNumSamples = 1;
  std::vector<M_t> Estimate(const std::vector<X_t>& A,
                            const std::vector<Y_t>& B) override {
    double z = 0;
    for (const auto& pt : A) z += pt.z();
    z /= A.size();
    Eigen::Quaterniond result;
    result.x() = 0;
    result.y() = 0;
    result.z() = 1;
    result.w() = -z;
    result.normalize();
    return {result};
  }
};

template<typename Point>
class VerticalPlaneEstimator : public PlaneEstimator<Point> {
 public:
  VerticalPlaneEstimator() = default;
  using typename PlaneEstimator<Point>::X_t;
  using typename PlaneEstimator<Point>::Y_t;
  using typename PlaneEstimator<Point>::M_t;
  static const int kMinNumSamples = 2;
  std::vector<M_t> Estimate(const std::vector<X_t>& A,
                            const std::vector<Y_t>& B) override {
// project A to xy plane
    std::vector<Eigen::Vector2d> pts;
    for (const auto& pt : A) {
      pts.emplace_back(pt.x(), pt.y());
    }

    Eigen::Vector2d mean = Eigen::Vector2d::Zero();
    for (const auto& pt : pts) {
      mean += pt;
    }
    mean /= pts.size();
    Eigen::Matrix2d H(2, pts.size());
    for (int i = 0; i < pts.size(); i++) {
      H.col(i) = pts[i] - mean;
    }
    Eigen::Matrix2d cov = H * H.transpose();
    Eigen::SelfAdjointEigenSolver<Eigen::Matrix2d> eigensolver(cov);
    Eigen::Vector2d eigenvalues = eigensolver.eigenvalues();
    Eigen::Matrix2d eigenvectors = eigensolver.eigenvectors();
    int min_index;
    double min_eigenvalue = eigenvalues.minCoeff(&min_index);
    Eigen::Vector2d main_direction = eigenvectors.col(min_index);

    Eigen::Vector3d normal = Eigen::Vector3d(main_direction(0), main_direction(1), 0);
    double d = 0;
    for (int i = 0; i < A.size(); i++) {
      d -= (normal.x() * A[i].x() + normal.y() * A[i].y());
    }
    d /= A.size();
    Eigen::Quaterniond result;
    result.vec() = normal;
    result.w() = d;
    result.normalize();
    return {result};
  }
};

class Point2PlaneError : public ceres::SizedCostFunction<1, 4> {
 public:
  explicit Point2PlaneError(const Eigen::Vector3d& point) : point_(point) {}

  bool Evaluate(double const* const* parameters,
                        double* residuals,
                        double** jacobians) const override {
    Eigen::Map<const Eigen::Quaterniond> q(parameters[0]);
    Eigen::Map<const Eigen::Vector3d> coeffs(parameters[0]);
    double d = coeffs.dot(point_) + q.w();
    double inv_norm = 1.0 / coeffs.norm();
    residuals[0] = d * inv_norm;
    if (jacobians && jacobians[0]) {
      Eigen::Map<Eigen::Matrix<double, 1, 4>> J(jacobians[0]);
      Eigen::Matrix<double, 1, 4> dd;
      dd = point_.homogeneous().transpose();
      Eigen::Matrix<double, 1, 4> dinv_norm = Eigen::Matrix<double, 1, 4>::Zero();
      dinv_norm.head(3) = (-2.0 * inv_norm * inv_norm * inv_norm) * coeffs;
      J = dd * inv_norm + dinv_norm * d;
    }
    return true;
  }
 private:
  Eigen::Vector3d point_;
};

class PlaneManifold : public ceres::Manifold {
 public:
  virtual bool Plus(const double* x, const double* delta, double* x_plus_delta) const {
    Eigen::Quaterniond dq = Eigen::Quaterniond(1.0, 0.5 * delta[0], 0.5 * delta[1], 0.5 * delta[2]);
    dq.normalize();
    Eigen::Map<const Eigen::Quaterniond> q(x);
    Eigen::Map<Eigen::Quaterniond> dq_plus_q(x_plus_delta);
    dq_plus_q = dq * q;
    dq_plus_q.normalize();
    return true;
  }
  virtual bool PlusJacobian(const double* x, double* jacobian) const {
    Eigen::Map<Eigen::Matrix<double, 4, 3, Eigen::RowMajor>> j(jacobian);
    Eigen::Map<const Eigen::Quaterniond> q(x);
    Eigen::Matrix4d Qright = math::QuaternionRight(q);
    j.setZero();
    j.block<3, 3>(0, 0) = Qright.block<3, 3>(1, 1);
    return true;
  }
  virtual int AmbientSize() const { return 4; }
  virtual int TangentSize() const { return 2; }
  virtual bool Minus(const double* y, const double* x, double* y_minus_x) const { return false; }
  virtual bool MinusJacobian(const double* x, double* jacobian) const { return false; }
};

class HorizontalPlaneManifold : public ceres::Manifold {
 public:
  virtual bool Plus(const double* x, const double* delta, double* x_plus_delta) const {
    Eigen::Map<const Eigen::Vector4d> q(x);
    Eigen::Map<Eigen::Vector4d> dq_plus_q(x_plus_delta);
    dq_plus_q = q;
    dq_plus_q.w() += delta[0];
    dq_plus_q.normalize();
    return true;
  }
  virtual bool PlusJacobian(const double* x, double* jacobian) const {
    Eigen::Map<Eigen::Vector4d> j(jacobian);
    j << 0, 0, 0, 1.0;
    return true;
  }
  virtual int AmbientSize() const { return 4; }
  virtual int TangentSize() const { return 1; }
  virtual bool Minus(const double* y, const double* x, double* y_minus_x) const { return false; }
  virtual bool MinusJacobian(const double* x, double* jacobian) const { return false; }
};

class VerticalPlaneManifold : public ceres::Manifold {
 public:
  virtual bool Plus(const double* x, const double* delta, double* x_plus_delta) const {
    Eigen::Map<Eigen::Vector4d> inc_x(x_plus_delta);
    Eigen::Map<const Eigen::Vector4d> x_vec(x);
    Eigen::Matrix3d rot =
        Eigen::Matrix3d::Identity() + 0.5 * math::SkewSymmetric(Eigen::Vector3d(0, 0, delta[0]));
    inc_x.head(3) = rot * x_vec.head(3);
    inc_x[3] = x_vec[3] + delta[1];
    inc_x.normalize();
    return true;
  }
  virtual bool PlusJacobian(const double* x, double* jacobian) const {
    Eigen::Map<Eigen::Matrix<double, 4, 2, Eigen::RowMajor>> j(jacobian);
    j.setZero();
    j(0, 0) = -0.5 * x[1];
    j(1, 0) = 0.5 * x[0];
    j(3, 1) = 1.0;
    return true;
  }
  virtual int AmbientSize() const { return 4; }
  virtual int TangentSize() const { return 2; }
  virtual bool Minus(const double* y, const double* x, double* y_minus_x) const { return false; }
  virtual bool MinusJacobian(const double* x, double* jacobian) const { return false; }
};
}  // namespace dm::recon

#endif  // VIO_RECON_PLANE_CORE_H_

