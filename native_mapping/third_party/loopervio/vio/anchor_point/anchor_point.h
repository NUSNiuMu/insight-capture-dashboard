#ifndef VIO_ANCHOR_POINT_ANCHOR_POINT_H_
#define VIO_ANCHOR_POINT_ANCHOR_POINT_H_

#include <glog/logging.h>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <unordered_map>
#include <vector>
#include "vio/data_types.h"

namespace dm {
namespace vio {

class Subdiv2DIndex : public cv::Subdiv2D {
 public:
  explicit Subdiv2DIndex(cv::Rect rectangle);

  // Source code of Subdiv2D:
  // https://github.com/opencv/opencv/blob/master/modules/imgproc/src/subdivision2d.cpp#L762 The
  // implementation tweaks getTrianglesList() so that only the indice of the triangle inside the
  // image are returned
  void getTrianglesIndices(std::vector<int>* ind) const;
};

class AnchorPoint {
 public:
  struct AnchorResult {
    Eigen::Vector3d pt;
    float dist2d;
    float confidence = 0;
  };
  using Ptr = std::shared_ptr<AnchorPoint>;
  AnchorPoint() {}
  AnchorPoint(const common::ImageUndistortion& intrinsic, const dm::math::Rigid3d& extrinsic)
      : intrinsic_(intrinsic), extrinsic_(extrinsic) {}

  std::optional<AnchorResult> GetPoint3d(float x, float y, Timestamp stamp);

  std::optional<AnchorResult> GetPoint3dByIdx(float x, float y, uint32_t idx);

  void AddVioPoint3ds(const std::vector<Point3d>& pts);

  void AddKeyFramePose(const std::map<Timestamp, dm::math::Rigid3d>& kf_poses);

 private:
  bool IsPointInsideTriangle(const cv::Point2f& pt, const cv::Point2f& v1, const cv::Point2f& v2,
                             const cv::Point2f& v3);

  Timestamp FindNearestKeyFrame(Timestamp stamp);

  size_t duration_time_ = 5e9;
  std::mutex vio_point3ds_mutex_;
  std::unordered_map<FeatureId, Point3d> vio_point3ds_;

  std::mutex kf_poses_mutex_;
  std::map<Timestamp, dm::math::Rigid3d> kf_poses_;
  dm::common::ImageUndistortion intrinsic_;
  dm::math::Rigid3d extrinsic_;
  float max_dist_ = 100;
};

}  // namespace vio
}  // namespace dm

#endif  // VIO_ANCHOR_POINT_ANCHOR_POINT_H_
