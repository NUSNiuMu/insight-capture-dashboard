#include "common/image_undistortion.h"

#include <algorithm>
#include <fstream>
#include <memory>
#include <string>

#include "common/equidistant_distortion.h"
#include "common/radial_tangential_distortion.h"
#include "glog/logging.h"

namespace dm {
namespace common {

ImageUndistortion::ImageUndistortion(const ImageUndistortion& other) {
  width_ = other.width_;
  height_ = other.height_;
  camera_model_ = other.camera_model_;
  intrinsic_ = other.intrinsic_;
  if (other.distortion_) {
    distortion_ = createNewDistortion(other);
  } else {
    distortion_ = nullptr;
  }
}

ImageUndistortion& ImageUndistortion::operator=(const ImageUndistortion& other) {
  if (this != &other) {
    width_ = other.width_;
    height_ = other.height_;
    camera_model_ = other.camera_model_;
    intrinsic_ = other.intrinsic_;
    if (other.distortion_) {
      distortion_ = createNewDistortion(other);
    } else {
      distortion_ = nullptr;
    }
  }
  return *this;
}

std::shared_ptr<Distortion> ImageUndistortion::createNewDistortion(const ImageUndistortion& other) {
  std::shared_ptr<Distortion> new_distortion;
  auto distortion_coef = other.distortion_coefficients();
  switch (other.camera_model_) {
    case dm::vio::CameraModel::PINHOLE:
      new_distortion = std::make_shared<RadialTangentialDistortion>(
          distortion_coef[0], distortion_coef[1], distortion_coef[2], distortion_coef[3]);
      break;
    case dm::vio::CameraModel::FISHEYE:
      new_distortion = std::make_shared<EquidistantDistortion>(
          distortion_coef[0], distortion_coef[1], distortion_coef[2], distortion_coef[3]);
      break;
    default:
      CHECK(0) << "invalidate camera model";
      break;
  }
  return new_distortion;
}

void ImageUndistortion::Init(const dm::vio::CameraCalibration& camera_calibration) {
  // equidistance: (k1, k2, k3, k4). Refer to
  // https://docs.opencv.org/4.5.1/db/d58/group__calib3d__fisheye.html
  // radialtangential: (k1, k2, p1, p2).

  width_ = camera_calibration.width;
  height_ = camera_calibration.height;
  intrinsic_ = camera_calibration.intrinsics;

  if (camera_calibration.distortion_coef.size() == 4) {
    camera_model_ = camera_calibration.camera_model;
    switch (camera_model_) {
      case dm::vio::CameraModel::PINHOLE:
        distortion_ = std::make_shared<RadialTangentialDistortion>(
            camera_calibration.distortion_coef[0], camera_calibration.distortion_coef[1],
            camera_calibration.distortion_coef[2], camera_calibration.distortion_coef[3]);
        break;
      case dm::vio::CameraModel::FISHEYE:
        distortion_ = std::make_shared<EquidistantDistortion>(
            camera_calibration.distortion_coef[0], camera_calibration.distortion_coef[1],
            camera_calibration.distortion_coef[2], camera_calibration.distortion_coef[3]);
        break;
      default:
        CHECK(0) << "invalidate camera model";
        break;
    }
  } else {
    CHECK(0) << "invalidate distortion_coef for camera model";
  }
}

void ImageUndistortion::Init(size_t width, size_t height, const std::vector<double>& intrinsic,
                             const std::vector<double>& distortion, int camera_model) {
  width_ = width;
  height_ = height;
  CHECK_EQ(intrinsic.size(), 4);
  intrinsic_[0] = intrinsic[0];
  intrinsic_[1] = intrinsic[1];
  intrinsic_[2] = intrinsic[2];
  intrinsic_[3] = intrinsic[3];

  if (distortion.size() == 4) {
    camera_model_ = dm::vio::CameraModel(camera_model);
    switch (camera_model_) {
      case dm::vio::CameraModel::PINHOLE:
        distortion_ = std::make_shared<RadialTangentialDistortion>(distortion[0], distortion[1],
                                                                   distortion[2], distortion[3]);
        break;
      case dm::vio::CameraModel::FISHEYE:
        distortion_ = std::make_shared<EquidistantDistortion>(distortion[0], distortion[1],
                                                              distortion[2], distortion[3]);
        break;
      default:
        CHECK(0) << "invalidate camera model";
        break;
    }
  }
}

void ImageUndistortion::Update(const Eigen::Vector4d& intrinsic) { intrinsic_ = intrinsic; }

void ImageUndistortion::Update(const Eigen::Vector4d& intrinsic,
                               const std::vector<double>& distortion) {
  intrinsic_ = intrinsic;
  distortion_->Update(distortion);
}

Eigen::Vector4d ImageUndistortion::intrinsic() const {
  return intrinsic_;
}

std::vector<double> ImageUndistortion::distortion_coefficients() const {
  return distortion_->GetParameters();
}

std::vector<Eigen::Vector2d> ImageUndistortion::LiftPointsToRays(
    const std::vector<Eigen::Vector2d>& points) const {
  std::vector<Eigen::Vector2d> rays;
  rays.resize(points.size());
  for (size_t i = 0, iend = points.size(); i < iend; ++i) {
    const auto& pt = points[i];
    rays[i] = Eigen::Vector2d((pt[0] - cx()) / fx(), (pt[1] - cy()) / fy());
  }

  return distortion_ ? distortion_->UndistortPoints(rays) : rays;
}

std::vector<cv_mini::Point2f> ImageUndistortion::LiftPointsToRays(
    const std::vector<cv_mini::Point2f>& points) const {

  std::vector<cv_mini::Point2f> rays;
  rays.resize(points.size());
  for (size_t i = 0, iend = points.size(); i < iend; ++i) {
    rays[i] = LiftPointToRay(points[i]);
  }
  return rays;
}

cv_mini::Point2f ImageUndistortion::LiftPointToRay(const cv_mini::Point2f& point) const {
  cv_mini::Point2f ray((point.x - cx()) / fx(), (point.y - cy()) / fy());
  if (distortion_) {
    Eigen::Vector2d result = distortion_->UndistortPoint(Eigen::Vector2d(ray.x, ray.y));
    ray.x = result.x();
    ray.y = result.y();
  }
  return ray;
}

Eigen::Vector2d ImageUndistortion::LiftPointToRay(const Eigen::Vector2d& point) const {
  cv_mini::Point2f cv_pt = LiftPointToRay(cv_mini::Point2f(point.x(), point.y()));
  return Eigen::Vector2d(cv_pt.x, cv_pt.y);
}

std::vector<cv_mini::Point2f> ImageUndistortion::ProjectRaysToPoints(
    const std::vector<cv_mini::Point2f>& rays) const {
  std::vector<cv_mini::Point2f> points = rays;
  if (distortion_) {
    points = distortion_->DistortPoints(rays);
  }

  for (size_t i = 0, iend = rays.size(); i < iend; ++i) {
    cv_mini::Point2f p = points[i];
    double x = fx() * p.x + cx();
    double y = fy() * p.y + cy();
    points[i] = cv_mini::Point2f(x, y);
  }
  return points;
}

Eigen::Vector2d ImageUndistortion::ProjectRayToPoint(const Eigen::Vector2d& ray) const {
  Eigen::Vector2d point = ray;
  if (distortion_) {
    point = distortion_->DistortPoint(ray);
  }

  double x = fx() * point.x() + cx();
  double y = fy() * point.y() + cy();
  return Eigen::Vector2d(x, y);
}

bool ImageUndistortion::IsInFrustum(const Eigen::Vector3d& pt3d, float z_min, float z_max) {
  if (pt3d.z() < z_min || pt3d.z() > z_max) {
    return false;
  }
  Eigen::Vector2d pt2d = ProjectRayToPoint(pt3d.head(2) / pt3d.z());
  return InBorder(pt2d);
}

void ImageUndistortion::GetParams(double* intrinsics) {
  intrinsics[0] = intrinsic_[0];
  intrinsics[1] = intrinsic_[1];
  intrinsics[2] = intrinsic_[2];
  intrinsics[3] = intrinsic_[3];
  if (distortion_) {
    const auto& distortion_params = distortion_->GetParameters();
    intrinsics[4] = distortion_params[0];
    intrinsics[5] = distortion_params[1];
    intrinsics[6] = distortion_params[2];
    intrinsics[7] = distortion_params[3];
  } else {
    intrinsics[4] = 0.0;
    intrinsics[5] = 0.0;
    intrinsics[6] = 0.0;
    intrinsics[7] = 0.0;
  }
}

bool ImageUndistortion::InBorder(const Eigen::Vector2d& uv, const uint32_t borderSize) const {
  int img_u = std::floor(uv[0] + 0.5);
  int img_v = std::floor(uv[1] + 0.5);
  int min_u = static_cast<int>(borderSize);
  int max_u = static_cast<int>(width_ - borderSize);
  int min_v = static_cast<int>(borderSize);
  int max_v = static_cast<int>(height_ - borderSize);

  return min_u <= img_u && img_u < max_u && min_v <= img_v && img_v < max_v;
}

}  // namespace common
}  // namespace dm
