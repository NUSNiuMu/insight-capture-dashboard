#ifndef VIO_FRONTEND_ORB_EXTRACTOR_H_
#define VIO_FRONTEND_ORB_EXTRACTOR_H_

#include <list>
#include <memory>
#include <vector>

#include "opencv2/opencv.hpp"

#include "vio/frontend/frontend_config.h"
#include "glog/logging.h"
#include "common/timer.h"

namespace dm {
namespace vio {

class ExtractorNode {
 public:
  ExtractorNode() : no_more_(false) {}

  void DivideNode(ExtractorNode* n1, ExtractorNode* n2, ExtractorNode* n3, ExtractorNode* n4);

  std::vector<cv::KeyPoint> keys_;
  cv::Point2i ul_, ur_, bl_, br_;  // left up, right bottom
  std::list<ExtractorNode>::iterator lit_;
  bool no_more_;
};

class ORBextractor {
 public:
  using Ptr = std::shared_ptr<ORBextractor>;

  ORBextractor(int features_num_, float scale_factor_, int levels_, int ini_fast_threshold_,
               int min_fast_threshold_);

  explicit ORBextractor(const dm::vio::OrbExtractorConfig& config)
      : ORBextractor(config.features_num, config.pyramid_scale_factor, config.pyramid_levels,
                     config.init_th_fast, config.min_th_fast) {}

  ~ORBextractor() {}

  // Compute the ORB features and descriptors on an image.
  // ORB are dispersed on the image using an octree.
  // Mask is ignored in the current implementation.
  void operator()(const cv::Mat& image, const cv::Mat& mask, std::vector<cv::KeyPoint>* keypoints,
                  cv::Mat* descriptors, bool blur = false);

  void DetectAndCompute(const cv::Mat& image, std::vector<cv::KeyPoint>* keypoints,
                        cv::Mat* descriptors, cv::Mat mask = cv::Mat(), bool blur = false) {
    (*this)(image, mask, keypoints, descriptors, blur);
  }

  int inline GetLevels() { return levels_; }

  float inline GetScaleFactor() { return scale_factor_; }

  std::vector<float> inline GetScaleFactors() { return scale_factors_; }

  std::vector<float> inline GetInverseScaleFactors() { return inv_scale_factors_; }

  std::vector<float> inline GetScaleSigmaSquares() { return level_sigma2_; }

  std::vector<float> inline GetInverseScaleSigmaSquares() { return inv_level_sigma2_; }

  // Compute the descriptors for the specified keypoints
  void GetDescriptors(const cv::Mat& image, std::vector<cv::KeyPoint>* keypoints,
                      cv::Mat* descriptors);

  int DescriptorDistance(const cv::Mat& a, const cv::Mat& b);

  std::vector<cv::Mat> image_pyramids_;

 protected:
  void ComputePyramid(const cv::Mat& image);
  void ComputeKeyPointsOctTree(std::vector<std::vector<cv::KeyPoint>>* allKeypoints);
  std::vector<cv::KeyPoint> DistributeOctTree(const std::vector<cv::KeyPoint>& vToDistributeKeys,
                                              const int& minX, const int& maxX, const int& minY,
                                              const int& maxY, const int& nFeatures,
                                              const int& level);

  std::vector<cv::Point> pattern;
  int features_num_;
  double scale_factor_;
  int levels_;
  int ini_fast_threshold_;
  int min_fast_threshold_;
  std::vector<int> features_per_level_;
  std::vector<int> umax_;
  std::vector<float> scale_factors_;
  std::vector<float> inv_scale_factors_;
  std::vector<float> level_sigma2_;
  std::vector<float> inv_level_sigma2_;
};

}  // namespace vio
}  // namespace dm

#endif  // VIO_FRONTEND_ORB_EXTRACTOR_H_
