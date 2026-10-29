#ifndef VIO_VIO_SYSTEM_STATUS_H_
#define VIO_VIO_SYSTEM_STATUS_H_

#include <string>
#include <unordered_map>

namespace dm {
namespace vio {

enum VioSystemStatus : int {
  VIO_NULL = -2,
  VIO_ESTIMATOR_NULL = -1,
  NOT_INITED = 0,
  TRACKING = 1,
  TRACKING_LOST = 2,
  DATA_LOST = 3,
  TRACKING_STATIC = 4,
  MOVING_ENVIRONMENT = 5,
  RELOCALIZATION_SUCCEED = 20,
  RELOCALIZATION_PROCESSING = 21,
  RELOCALIZATION_BAD_IMAGE = 22,
  RELOCALIZATION_MOVE_TOO_FAST = 23,
  RELOCALIZATION_NOT_FOUND_IN_MAP = 24,
};

struct VioStatusInfo {
  VioSystemStatus code;
  std::string msg;
};

inline bool ContinueRelocalizing(const VioSystemStatus& status) {
  return status >= 21 && status < 30;
}

inline bool IsTracking(const VioSystemStatus& status) {
  return status == TRACKING || status == TRACKING_STATIC;
}

extern const std::unordered_map<VioSystemStatus, std::string> vio_status_msg;

}  // namespace vio
}  // namespace dm

#endif  // VIO_VIO_SYSTEM_STATUS_H_
