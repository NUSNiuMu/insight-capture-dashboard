#pragma once
#include <algorithm>
#include <cmath>
#include <limits>
#include <optional>
#include <vector>
#include <Eigen/Geometry>
#include <Eigen/SVD>

namespace insight {
struct RigidAlignment {
  std::optional<Eigen::Matrix4d> pose;
  const char* reason = "insufficient_retained_landmarks";
  size_t inliers = 0;
  double rms_m = std::numeric_limits<double>::quiet_NaN();
  double max_m = std::numeric_limits<double>::quiet_NaN();
  double spread_ratio = std::numeric_limits<double>::quiet_NaN();
};

inline RigidAlignment AlignRetainedLandmarks(const std::vector<Eigen::Vector3d>& source,
                                           const std::vector<Eigen::Vector3d>& target) {
  RigidAlignment result;
  if(source.size()<10 || source.size()!=target.size()) return result;
  Eigen::Matrix3Xd from(3,source.size()), to(3,target.size());
  for(size_t i=0;i<source.size();++i) { from.col(i)=source[i]; to.col(i)=target[i]; }
  result.reason="nonfinite_retained_landmarks";
  if(!from.allFinite() || !to.allFinite()) return result;
  Eigen::VectorXd weights=Eigen::VectorXd::Ones(source.size());
  Eigen::Matrix4d pose=Eigen::Matrix4d::Identity();
  // Huber reweighting prevents a few deformed landmarks from moving the whole fit.
  for(int iteration=0;iteration<5;++iteration) {
    const Eigen::Vector3d a=from*weights/weights.sum(), b=to*weights/weights.sum();
    Eigen::Matrix3Xd centered=from.colwise()-a;
    centered.array().rowwise()*=weights.transpose().array();
    const Eigen::Matrix3d covariance=centered*(to.colwise()-b).transpose();
    Eigen::JacobiSVD<Eigen::Matrix3d> svd(covariance,Eigen::ComputeFullU|Eigen::ComputeFullV);
    Eigen::Matrix3d sign=Eigen::Matrix3d::Identity();
    sign(2,2)=(svd.matrixV()*svd.matrixU().transpose()).determinant()<0 ? -1. : 1.;
    pose.topLeftCorner<3,3>()=svd.matrixV()*sign*svd.matrixU().transpose();
    pose.topRightCorner<3,1>()=b-pose.topLeftCorner<3,3>()*a;
    const Eigen::Matrix3Xd error=((pose.topLeftCorner<3,3>()*from).colwise()+pose.topRightCorner<3,1>())-to;
    weights=(.003/error.colwise().norm().array().max(.003)).matrix().transpose();
  }
  result.reason="invalid_rigid_alignment";
  if(!pose.allFinite()) return result;
  const Eigen::Matrix3Xd error=((pose.topLeftCorner<3,3>()*from).colwise()+pose.topRightCorner<3,1>())-to;
  Eigen::Matrix3Xd supported_from(3,source.size()),supported_to(3,source.size());
  for(size_t i=0;i<source.size();++i) {
    if(error.col(i).norm()>.010) continue;
    supported_from.col(result.inliers)=from.col(i);
    supported_to.col(result.inliers++)=to.col(i);
  }
  result.reason="insufficient_alignment_inliers";
  if(result.inliers<10 || result.inliers<source.size()*.8) return result;
  from=supported_from.leftCols(result.inliers).eval();
  to=supported_to.leftCols(result.inliers).eval();
  result.reason="degenerate_retained_landmarks";
  result.spread_ratio=1.;
  for(const Eigen::Matrix3Xd* points:{&from,&to}) {
    Eigen::Matrix3Xd centered=points->colwise()-points->rowwise().mean();
    const auto singular=centered.jacobiSvd().singularValues().eval();
    if(!singular.allFinite()) return result;
    // Planar geometry is observable; a line or a tiny cluster is not.
    const double ratio=singular[0]>0 ? singular[1]/singular[0] : 0.;
    result.spread_ratio=std::min(result.spread_ratio,ratio);
    if(ratio<.01 || singular[1]/std::sqrt(result.inliers)<.01) return result;
  }
  pose=Eigen::umeyama(from,to,false);
  result.reason="invalid_rigid_alignment";
  if(!pose.allFinite()) return result;
  Eigen::Matrix3Xd residual=(pose.topLeftCorner<3,3>()*from).colwise()+pose.topRightCorner<3,1>();
  residual-=to;
  result.rms_m=std::sqrt(residual.squaredNorm()/result.inliers);
  result.max_m=residual.colwise().norm().maxCoeff();
  result.reason="nonrigid_landmark_update";
  if(!std::isfinite(result.rms_m) || !std::isfinite(result.max_m) ||
     result.rms_m>.003 || result.max_m>.010) return result;
  result.reason="accepted";
  result.pose=pose;
  return result;
}
}
