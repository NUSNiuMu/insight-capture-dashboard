#ifndef VIO_BRIEF_BRIEF_H_
#define VIO_BRIEF_BRIEF_H_

#include <vector>
#include <opencv2/opencv.hpp>

namespace dm {
namespace vio {

// BRIEF descriptor
class Brief {
 public:
  // Compute brief descriptor
  void Compute(const cv::Mat& image, const std::vector<cv::KeyPoint>& points, cv::Mat* descriptors,
               bool treat_image = true) const;

  void ImportPattern(const std::vector<int>& x1, const std::vector<int>& y1,
                          const std::vector<int>& x2, const std::vector<int>& y2);
 protected:
  // Coordinates of test points relative to the center point
  std::vector<int> x1_, x2_;
  std::vector<int> y1_, y2_;
};
}  // namespace vio
}  // namespace dm

#endif  // VIO_BRIEF_BRIEF_H_
