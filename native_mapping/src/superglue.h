#pragma once
#include "vio/map/key_frame.h"
#include "vio/map/map_point.h"

namespace insight {
std::vector<dm::vio::lm::MapPoint*> MatchReference(
    dm::vio::lm::KeyFrame* query, dm::vio::lm::KeyFrame* reference);
}
