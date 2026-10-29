#ifndef VIO_EXT_RIGID_H_
#define VIO_EXT_RIGID_H_

#include <Eigen/Dense>

#include <cmath>
#include <iostream>
#include <memory>
#include <string>

namespace dm {
namespace math {

template <typename FloatType>
class Rigid3 {
 public:
  using Vector = Eigen::Matrix<FloatType, 3, 1>;
  using Quaternion = Eigen::Quaternion<FloatType>;
  using AngleAxis = Eigen::AngleAxis<FloatType>;

  Rigid3() : translation_(Vector::Zero()), rotation_(Quaternion::Identity()) {}

  Rigid3(const Vector& translation, const Quaternion& rotation)
      : translation_(translation), rotation_(rotation) {}

  Rigid3(const Vector& translation, const AngleAxis& rotation)
      : translation_(translation), rotation_(rotation) {}

  static Rigid3 FromRotation(const AngleAxis& angle_axis) {
    return Rigid3(Vector::Zero(), Quaternion(angle_axis));
  }

  static Rigid3 FromRotation(const Quaternion& rotation) {
    return Rigid3(Vector::Zero(), rotation);
  }

  static Rigid3 FromTranslation(const Vector& vector) {
    return Rigid3(vector, Quaternion::Identity());
  }

  static Rigid3 FromArrays(const std::array<FloatType, 4>& rotation,
                           const std::array<FloatType, 3>& translation) {
    return Rigid3(Eigen::Map<const Vector>(translation.data()),
                  Eigen::Quaternion<FloatType>(rotation[0], rotation[1], rotation[2], rotation[3]));
  }

  static Rigid3 FromMatrix(const Eigen::Matrix<FloatType, 4, 4>& matrix) {
    Eigen::Matrix3d rotation_matrix = matrix.topLeftCorner(3, 3);
    Eigen::Vector3d translation_vec;
    for (int i = 0; i < 3; i++) {
      translation_vec(i) = matrix(i, 3);
    }
    return Rigid3(translation_vec, Eigen::Quaterniond(rotation_matrix));
  }

  static Rigid3<FloatType> Identity() { return Rigid3<FloatType>(); }

  const Vector& translation() const { return translation_; }
  Vector& translation() { return translation_; }

  const Quaternion& rotation() const { return rotation_; }
  Quaternion& rotation() { return rotation_; }

  template <typename OtherType>
  Rigid3<OtherType> Cast() const {
    return Rigid3<OtherType>(translation().template cast<OtherType>,
                             rotation().template cast<OtherType>);
  }

  Rigid3 Inverse() const {
    const Quaternion rotation_new = rotation_.conjugate();
    const Vector translation_new = -(rotation_new * translation_);
    return Rigid3(translation_new, rotation_new);
  }

  Eigen::Transform<FloatType, 3, Eigen::Affine> EigenTransform() const {
    return Eigen::Translation<FloatType, 3>(translation()) * rotation();
  }

  Eigen::Matrix<FloatType, 4, 4> Matrix44() const {
    Eigen::Matrix<FloatType, 4, 4> homogeneous_matrix;
    homogeneous_matrix.setZero();
    homogeneous_matrix.block(0, 0, 3, 3) = rotation_.toRotationMatrix();
    homogeneous_matrix.col(3) =
        Eigen::Matrix<FloatType, 4, 1>(translation_.x(), translation_.y(), translation_.z(), 1.0f);
    return homogeneous_matrix;
  }

  Eigen::Matrix<FloatType, 3, 4> Matrix34() const {
    Eigen::Matrix<FloatType, 3, 4> homogeneous_matrix;
    homogeneous_matrix.block(0, 0, 3, 3) = rotation_.toRotationMatrix();
    homogeneous_matrix.col(3) =
        Eigen::Matrix<FloatType, 3, 1>(translation_.x(), translation_.y(), translation_.z());
    return homogeneous_matrix;
  }

  // Calculate Essential Matrix E that
  // p2 = R * p1 + T => p2^T * E * p1 = 0
  Eigen::Matrix<FloatType, 3, 3> EssentialMatrix() const {
    Eigen::Matrix<FloatType, 3, 3> cpt;
    cpt << 0, -translation_.z(), translation_.y(), // NOLINT
           translation_.z(), 0, -translation_.x(), // NOLINT
           -translation_.y(), translation_.x(), 0;
    return cpt * rotation_.toRotationMatrix();
  }

  std::string ToString() const {
    std::stringstream ss;
    ss << *this;
    return ss.str();
  }

  bool IsValid() const {
    return !std::isnan(translation_.x()) && !std::isnan(translation_.y()) &&
           !std::isnan(translation_.z()) &&
           std::abs(FloatType(1) - rotation_.norm()) < FloatType(1e-3);
  }

 private:
  Vector translation_;
  Quaternion rotation_;

  EIGEN_MAKE_ALIGNED_OPERATOR_NEW

  // Allow shallow copy and assignment.
};

template <typename FloatType>
Rigid3<FloatType> operator*(const Rigid3<FloatType>& lhs, const Rigid3<FloatType>& rhs) {
  return Rigid3<FloatType>(lhs.rotation() * rhs.translation() + lhs.translation(),
                           (lhs.rotation() * rhs.rotation()).normalized());
}

template <typename FloatType>
typename Rigid3<FloatType>::Vector operator*(const Rigid3<FloatType>& rigid,
                                             const typename Rigid3<FloatType>::Vector& point) {
  return rigid.rotation() * point + rigid.translation();
}

template <typename T>
std::ostream& operator<<(std::ostream& os, const Rigid3<T>& rigid) {
  return os << "{ t: [" << rigid.translation().transpose() << "], q: ["
            << rigid.rotation().coeffs().transpose() << "] }";
}

using Rigid3d = Rigid3<double>;
using Rigid3f = Rigid3<float>;
}  // namespace math
}  // namespace dm

#endif  // VIO_EXT_RIGID_H_
