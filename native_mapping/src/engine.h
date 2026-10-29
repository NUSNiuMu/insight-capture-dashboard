#pragma once
#include <memory>
#include <map>
#include <nlohmann/json.hpp>
#include <opencv2/core.hpp>
#include "vio/map/local_mapping.h"
#include "vio/map/loop_closure.h"

namespace insight {
using Json = nlohmann::json;
class Engine {
 public:
  Json Initialize(const Json& config);
  Json Map(const Json& request, const std::vector<cv::Mat>& images);
  Json Localize(const Json& request, const std::vector<cv::Mat>& arrays);
  Json Stats() const;
  std::vector<cv::Mat> Geometry() const;
  void Reset();
 private:
  std::unique_ptr<dm::vio::lm::LocalMapping> mapping_;
  std::unique_ptr<dm::vio::lm::LoopClosure> loops_;
  uint64_t last_map_stamp_ = 0;
  uint64_t map_revision_ = 0;
  struct Query {
    Json request;
    dm::vio::lm::KeyFrame* anchor;
    std::vector<std::pair<dm::vio::FeatureId, Eigen::Vector3d>> imu_points;
  };
  std::map<int, Query> queries_;
};
}
