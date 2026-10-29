#ifndef COMMON_IMAGE_UNDISTORTION_H_
#define COMMON_IMAGE_UNDISTORTION_H_
#include <Eigen/Dense>
#include <string>
#include <vector>
#ifdef __EMSCRIPTEN__
#include "cv_mini/cv_mini.hpp"
#else
#include <opencv2/opencv.hpp>
#define cv_mini cv
#endif
#include "common/distortion.h"
#include "config/sensor_calibration.h"

namespace dm {
namespace common {

class ImageUndistortion {
 public:
  ImageUndistortion() {}
  ImageUndistortion(const ImageUndistortion& other);
  ImageUndistortion& operator=(const ImageUndistortion& other);
  void Init(const dm::vio::CameraCalibration& camera_calibration);
  void Init(size_t width, size_t height, const std::vector<double>& intrinsic,
            const std::vector<double>& distortion,
            int camera_model = dm::vio::CameraModel::FISHEYE);
  void Update(const Eigen::Vector4d& intrinsic);
  void Update(const Eigen::Vector4d& intrinsic, const std::vector<double>& distortion);
  // Lift the points from the image plane to their projective rays. They will
  // also be undistorted in the process if there is a distortion.
  std::vector<Eigen::Vector2d> LiftPointsToRays(const std::vector<Eigen::Vector2d>& points) const;
  std::vector<cv_mini::Point2f> LiftPointsToRays(const std::vector<cv_mini::Point2f>& points) const;
  cv_mini::Point2f LiftPointToRay(const cv_mini::Point2f& point) const;
  Eigen::Vector2d LiftPointToRay(const Eigen::Vector2d& point) const;
  // Project the rays on the image plane to get the points. They will also be
  // distorted in the process if there is a distortion.
  std::vector<cv_mini::Point2f> ProjectRaysToPoints(
      const std::vector<cv_mini::Point2f>& rays) const;
  Eigen::Vector2d ProjectRayToPoint(const Eigen::Vector2d& ray) const;
  // Get the camera intrinsics
  void GetParams(double* intrinsics);

  bool InBorder(const Eigen::Vector2d& pt, const uint32_t borderSize = 1U) const;

  uint32_t width() const { return width_; }
  uint32_t height() const { return height_; }
  inline double fx() const { return intrinsic_[0]; }
  inline double fy() const { return intrinsic_[1]; }
  inline double cx() const { return intrinsic_[2]; }
  inline double cy() const { return intrinsic_[3]; }
  inline double f() const { return (fx() + fy()) / 2; }
  dm::vio::CameraModel camera_model() const { return camera_model_; }

  Eigen::Vector4d intrinsic() const;
  std::vector<double> distortion_coefficients() const;

  bool IsInFrustum(const Eigen::Vector3d& pt3d, float z_min = 0, float z_max = 100);

  friend std::ostream& operator<<(std::ostream& os, const ImageUndistortion& obj) {
    auto distortions = obj.distortion_coefficients();
    Eigen::Map<Eigen::Vector4d> distortions_vec(distortions.data());
    os << "width: " << obj.width() << " height: " << obj.height() << "\t"
       << "intrinsic: " << obj.fx() << " " << obj.fy() << " " << obj.cx() << " " << obj.cy() << "\t"
       << distortions_vec.transpose();
    return os;
  }

 private:
  Distortion::Ptr createNewDistortion(const ImageUndistortion& other);
  uint32_t width_ = 0;
  uint32_t height_ = 0;
  dm::vio::CameraModel camera_model_ = dm::vio::CameraModel::FISHEYE;
  Eigen::Vector4d intrinsic_;
  Distortion::Ptr distortion_ = nullptr;
};

}  // namespace common
}  // namespace dm

#endif  // COMMON_IMAGE_UNDISTORTION_H_
