#ifndef VIO_VIO_H_
#define VIO_VIO_H_
#include <Eigen/Core>
#ifdef __EMSCRIPTEN__
#include "cv_mini/cv_mini.hpp"
#else
#include "opencv2/opencv.hpp"
#include "vio/backend/frame_state.h"
#define cv_mini cv
#endif

namespace dm {
namespace vio {

// Interface for VIO.
class Vio {
 public:
  virtual ~Vio() = default;

  // Adds an image to Vio. This is a convenient method to be used when there is only one camera
  // in the system.
  virtual void AddImage(const uint64_t timestamp, const cv_mini::Mat& image) = 0;

  // Adds an image that is taken by the camera with camera_id to the system.
  virtual void AddImage(const uint32_t camera_id, const uint64_t timestamp,
                        const cv_mini::Mat& image) = 0;

  // Add an IMU measurement to the system.
  virtual void AddImuMeasurement(const uint64_t timestamp,
                                 const Eigen::Vector3d& linear_acceleration,
                                 const Eigen::Vector3d& angular_velocity) = 0;

  // Gets the most recent VIO FrameState.
  // If this state estimator has not initialized yet, we return a timestamp of 0 to indicate that.
  virtual void GetLatestStateEstimate(uint64_t* timestamp, dm::vio::FrameState* state) const = 0;
};

}  // namespace vio
}  // namespace dm

#endif  // VIO_VIO_H_
