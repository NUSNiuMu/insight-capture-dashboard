// Copyright 2020 DeepMirror Inc. All rights reserved.
#ifndef VIO_MAP_ORB_MATCHER_H_
#define VIO_MAP_ORB_MATCHER_H_

#include <memory>
#include <string>
#include <vector>
#include "vio/map/key_frame.h"
#include "vio/map/map_point.h"

namespace dm::vio::lm {

float CompueEpipolarDist(const Eigen::Vector2d& p0, const Eigen::Vector2d& p1,
                         const dm::math::Rigid3d& T10);

float CompueEpipolarDist(const Eigen::Vector2d& p0, const Eigen::Vector2d& p1,
                         const Eigen::Matrix3d& E10);

class ORBMatcher {
 public:
  using Ptr = std::shared_ptr<ORBMatcher>;
  /**
   * @brief Construct a new ORBMatcher object
   *
   * @param nnratio the ratio of best match and second match
   * @param check_ori check feature's orientation or not
   */

  ORBMatcher(int th_low = 50, int th_high = 150, float nnratio = 0.8,
             bool check_orientation = false)
      : TH_LOW(th_low),
        TH_HIGH(th_high),
        nnratio_(nnratio),
        check_orientation_(check_orientation) {}

  ~ORBMatcher() = default;

  // Computes the Hamming distance between two ORB descriptors
  static int DescriptorDistance(const cv::Mat& a, const cv::Mat& b);

  std::vector<cv::DMatch> MatchByVoc(KeyFrame* query_kf, CameraId query_cam, KeyFrame* train_kf,
                                     CameraId train_cam, bool check_orb_level = false,
                                     bool skip_mpts = false, bool strict = false);

  // Search matches between keyFrame keypoints(only master camera) and projected MapPoints. Returns
  // number of matches Used to track the local map (Tracking)
  int SearchByProjection(KeyFrame* F, const MapPointSet& map_points, float search_scale = 3,
                         bool far_points_limit = false,
                         float threshold_far_points = 50.0f);

  std::vector<cv::DMatch> SearchByProjection(KeyFrame* query_kf, CameraId query_cam,
                                             KeyFrame* train_kf, CameraId train_cam,
                                             float search_radius, bool bMono = false);

  // Matching to triangulate new MapPoints. Check Epipolar Constraint. only use master camera
  void SearchForTriangulation(KeyFrame* query_kf, KeyFrame* train_kf,
                              std::vector<cv::DMatch>* matches);

  std::vector<cv::DMatch> SearchByBoWForLoop(KeyFrame* query_kf,
                                             KeyFrame* loop_kf,
                                             std::vector<MapPoint*>* vpMatches);

  std::vector<cv::DMatch> SearchByBfForLoop(KeyFrame* query_kf,
                                            KeyFrame* loop_kf,
                                            std::vector<MapPoint*>* vpMatches);

  int SearchByProjectionForLoop(KeyFrame* query_kf,
                                const math::Rigid3d& Twi,
                                const MapPointSet& local_mpts,
                                std::vector<MapPoint *>* vpMatches,
                                float radius);

  // Project MapPoints into KeyFrame and search for duplicated MapPoints, and update map points's
  // descriptor
  int Fuse(KeyFrame* kf, const std::vector<MapPoint*>& map_points, float search_scale = 3.0,
           const std::string& tag = "");

  void ComputeThreeMaxima(std::vector<int>* histo, int L, int* ind1, int* ind2, int* ind3);

  void SetEpiportThreshold(float epiport_th) {
    epiport_th_ = epiport_th;
  }

 private:
  float nnratio_ = 0.8;
  bool check_orientation_ = false;

  const int TH_HIGH = 100;
  const int TH_LOW = 50;
  int HISTO_LENGTH = 30;
  static cv::Ptr<cv::BFMatcher> matcher_;
  float RadiusByViewingCos(float view_cos);

  double base_err_ = 3.0;
  float epiport_th_ = 5.0;
};

}  // namespace dm::vio::lm
#endif  // VIO_MAP_ORB_MATCHER_H_
