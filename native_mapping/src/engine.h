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
  Json Accept(const Json& request);
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
    dm::math::Rigid3d anchor_pose;
    std::unique_ptr<dm::vio::lm::KeyFrame> frame;
    std::vector<std::pair<size_t, dm::vio::FeatureId>> observations;
  };
  std::map<int, Query> queries_;
  std::map<int, Query> pending_queries_;
};
}
