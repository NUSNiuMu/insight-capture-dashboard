#pragma once
#include "vio/map/key_frame.h"
#include "vio/map/map_point.h"
#include <stdexcept>

namespace insight {
class InferenceUnavailable : public std::runtime_error {
 public:
  using std::runtime_error::runtime_error;
};
std::vector<dm::vio::lm::MapPoint*> MatchReference(
    dm::vio::lm::KeyFrame* query, dm::vio::lm::KeyFrame* reference);
}
