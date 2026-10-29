//
// Created by qiyong on 24-4-1.
//

#ifndef VIO_RECON_SIMPLE_PLANE_RECON_H_
#define VIO_RECON_SIMPLE_PLANE_RECON_H_

#include <memory>
#include <mutex>
#include <map>
#include <vector>
#include <opencv2/core/mat.hpp>
#include "Eigen/Eigen"
#include "common/image_undistortion.h"

namespace dm::recon {

struct Mesh {
  using Ptr = std::shared_ptr<Mesh>;
  Mesh() = default;
  Mesh(const std::vector<Eigen::Vector3f>& vertices, const std::vector<Eigen::Vector3i>& faces)
      : vertices(vertices), faces(faces) {}

  std::vector<Eigen::Vector3f> vertices;
  std::vector<Eigen::Vector3i> faces;

  // override +=
  Mesh& operator+=(const Mesh& other) {
    int vertex_num = vertices.size();
    vertices.insert(vertices.end(), other.vertices.begin(), other.vertices.end());
    Eigen::Vector3i inc(vertex_num, vertex_num, vertex_num);
    for (int i = 0; i < other.faces.size(); ++i) {
      faces.push_back(other.faces[i] + inc);
    }
    return *this;
  }

  // override operator +
  Mesh operator+(const Mesh& other) const {
    Mesh result = *this;
    int vertex_num = result.vertices.size();
    result.vertices.insert(result.vertices.end(), other.vertices.begin(), other.vertices.end());
    int face_num = result.faces.size();
    Eigen::Vector3i inc(vertex_num, vertex_num, vertex_num);
    for (int i = 0; i < other.faces.size(); ++i) {
      result.faces.push_back(other.faces[i] + inc);
    }
    return result;
  }
};

class SimpPlaneRecon {
 public:
  using Ptr = std::shared_ptr<SimpPlaneRecon>;
  SimpPlaneRecon() = default;
  ~SimpPlaneRecon() = default;

  void EstimatePlane(const std::vector<Eigen::Vector3d>& points);

  std::vector<Eigen::Quaterniond> GetPlanes() {
    std::lock_guard<std::mutex> lock(mutex_);
    return planes_;
  }

  cv::Mat DrawPlanes(const cv::Mat& img, const dm::common::ImageUndistortion& intrinsic,
                     const dm::math::Rigid3d& pose);

  cv::Mat DrawMeshes(const cv::Mat& img, const dm::common::ImageUndistortion& intrinsic,
                     const dm::math::Rigid3d& pose);

  Mesh GetMesh() {
    std::lock_guard<std::mutex> lock(mesh_mutex_);
    return mesh_;
  }

 private:
  Mesh ConvertPlaneToMesh(const Eigen::Quaterniond& plane,
                          const std::vector<Eigen::Vector3d>& inliers);

  bool DrawPlane(const std::vector<Eigen::Vector3d>& inliers,
                 const dm::common::ImageUndistortion& intrinsic,
                 const dm::math::Rigid3d& pose,
                 cv::Vec3b color,
                 cv::Mat* img);

  bool DrawMesh(const Mesh& mesh, const dm::common::ImageUndistortion& intrinsic,
                const dm::math::Rigid3d& pose, cv::Vec3b color, cv::Mat* img);

  std::mutex mutex_;
  std::vector<Eigen::Quaterniond> planes_;
  std::map<int, std::vector<Eigen::Vector3d>> inliers_;
  std::mutex mesh_mutex_;
  Mesh mesh_;
};

}  // namespace dm::recon
#endif  // VIO_RECON_SIMPLE_PLANE_RECON_H_
