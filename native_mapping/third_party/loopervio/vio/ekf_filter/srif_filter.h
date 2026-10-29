#ifndef VIO_EKF_FILTER_SRIF_FILTER_H_
#define VIO_EKF_FILTER_SRIF_FILTER_H_

#include <tuple>
#include <string>
#include <unordered_map>
#include <vector>
#include <unordered_set>
#include <memory>
#include "Eigen/Eigen"
#include "glog/logging.h"
#include "vio/data_types.h"
#include "common/timer.h"

namespace dm::vio {
#ifndef __EMSCRIPTEN__
template<typename Matrix>
cv::Mat CreateValueMap(const Matrix& mat) {
  cv::Mat cv_mat = cv::Mat::zeros(mat.rows(), mat.cols(), CV_8UC1);
  for (size_t r = 0; r < mat.rows(); r++) {
    for (size_t c = 0; c < mat.cols(); c++) {
      if (fabs(mat(r, c)) > 1e-8) cv_mat.at<uchar>(r, c) = 255;
    }
  }
  return cv_mat;
}

template<typename Matrix>
cv::Mat CreateColorValueMap(const Matrix& mat) {
  cv::Mat float_mat = cv::Mat::zeros(mat.rows(), mat.cols(), CV_32FC1);
  for (size_t r = 0; r < mat.rows(); r++) {
    for (size_t c = 0; c < mat.cols(); c++) {
      float v = fabs(mat(r, c));
      if (v <= 1e-8) continue;
      float_mat.at<float>(r, c) = v;
    }
  }
  cv::normalize(float_mat, float_mat, 0, 255, cv::NORM_MINMAX);
  float_mat.convertTo(float_mat, CV_8UC1);
  cv::Mat color_map;
  cv::applyColorMap(float_mat, color_map, cv::COLORMAP_JET);
  return color_map;
}
#endif

inline Eigen::MatrixXf TriangularSquare(const Eigen::RatrixXf& mat) {
  Eigen::MatrixXf result = Eigen::MatrixXf::Zero(mat.rows(), mat.cols());
  size_t length = mat.rows();
  for (size_t r = 0; r < result.rows(); r++) {
    for (size_t c = r; c < result.cols(); c++) {
      size_t l = length - c;
      result(r, c) = mat.row(r).tail(l).dot(mat.row(c).tail(l));
      result(c, r) = result(r, c);
    }
  }
  return result;
}

struct SrifResidualBlock {
  using Ptr = std::shared_ptr<SrifResidualBlock>;
  SrifResidualBlock(const std::unordered_map<BaseMeta::Ptr, Eigen::MatrixXf>& _x2phi,
                    const Eigen::VectorXf& _residual, const std::string& _name = std::string())
      : x2phi(_x2phi), residual(_residual), name(_name) {}
  SrifResidualBlock(const std::unordered_map<BaseMeta::Ptr, Eigen::MatrixXf>& _x2phi,
                    const Eigen::VectorXf& _residual,
                    const Eigen::MatrixXf& _sqrt_info,
                    const std::string& _name = std::string())
      : x2phi(_x2phi),
        residual(_residual),
        sqrt_info(_sqrt_info),
        name(_name),
        is_identity_info(false) {}
  std::unordered_map<BaseMeta::Ptr, Eigen::MatrixXf> x2phi;
  Eigen::VectorXf residual;
  Eigen::MatrixXf sqrt_info;
  std::string name;
  bool is_identity_info = true;
};

struct SlamFeatInitInfo {
  using Ptr = std::shared_ptr<SlamFeatInitInfo>;
  SlamFeatInitInfo() {}
  SlamFeatInitInfo(LandmarkMeta::Ptr _pt, const std::vector<BaseMeta::Ptr>& _hx_order,
                   const Eigen::MatrixXf& _Hx, const Eigen::MatrixXf& _Hf,
                   const Eigen::VectorXf& _res)
      : point(_pt), Hx_order(_hx_order), H_x(_Hx), H_f(_Hf), res(_res) {}
  LandmarkMeta::Ptr point;
  std::vector<BaseMeta::Ptr> Hx_order;
  Eigen::MatrixXf H_x;
  Eigen::MatrixXf H_f;
  Eigen::VectorXf res;
};

class SrifProblem {
 public:
  using Ptr = std::shared_ptr<SrifProblem>;
  void AddResidualBlock(SrifResidualBlock::Ptr residual_block) {
    for (auto& res : residuals_) {
      CHECK_NE(residual_block->name, res->name) << " the block have been added";
    }
    residuals_.push_back(residual_block);
  }

  std::tuple<Eigen::RatrixXf, Eigen::VectorXf> ConstructProblem(
      const std::vector<BaseMeta::Ptr>& orders, bool inverse = false);

  void Clear() { residuals_.clear(); }

  std::unordered_set<BaseMeta::Ptr> AllMetas() {
    std::unordered_set<BaseMeta::Ptr> metas;
    for (auto& residual : residuals_) {
      for (auto& x2phi : residual->x2phi) metas.insert(x2phi.first);
    }
    return metas;
  }

  SrifResidualBlock::Ptr GetResidual(const std::string& name) {
    for (auto& res : residuals_) {
      if (res->name == name) return res;
    }
    return nullptr;
  }

  [[nodiscard]] const std::vector<SrifResidualBlock::Ptr>& residuals() const { return residuals_; }

 private:
  std::vector<SrifResidualBlock::Ptr> residuals_;
};

class SRIFilter {
 public:
  using Ptr = std::shared_ptr<SRIFilter>;

  SRIFilter() {
    srif_problem_ = std::make_shared<SrifProblem>();
  }

  const std::vector<BaseMeta::Ptr>& variables() const { return variables_; }

  size_t VariablesDimension() {
    size_t d = 0;
    for (auto& var : variables_) d += var->size();
    return d;
  }

  SrifProblem::Ptr srif_problem() const { return srif_problem_; }

  inline size_t GetSlamSize() {
    size_t slam_size = 0;
    for (auto var : variables_) {
      if (var->derived_type != BaseMeta::LANDMARK) break;
      slam_size++;
    }
    return slam_size;
  }

  void RefreshVariableLocalId() {
    size_t cur_id = 0;
    for (auto var : variables_) {
      var->set_local_id(cur_id);
      cur_id += var->size();
    }
  }

  void InsertVariable(const BaseMeta::Ptr& data, int pos = -1) {
    if (pos == -1) {
      size_t local_id =
          variables_.empty() ? 0 : (variables_.back()->local_id() + variables_.back()->size());
      variables_.push_back(data);
      data->set_local_id(local_id);
    } else {
      CHECK_LT(pos, variables_.size());
      CHECK_GE(pos, 0);
      variables_.insert(variables_.begin() + pos, data);
    }
    new_variables_.insert(data);
  }

  BaseMeta::Ptr GetVariableByPosition(size_t pos) {
    CHECK(new_variables_.empty())
            << "new_variables_ size: " << new_variables_.size();
    for (auto& var : variables_) {
      if (var->local_id() + var->size() > pos) return var;
    }
    return nullptr;
  }

  const Eigen::MatrixXf& covariance() const {
    return covariance_;
  }

  const Eigen::MatrixXf& sqrt_info() const {
    return sqrt_info_;
  }

  Eigen::MatrixXf GetMarginalCovariance(
      const std::vector<BaseMeta::Ptr>& variables);

  void AddResidualBlock(SrifResidualBlock::Ptr residual_block) {
    srif_problem_->AddResidualBlock(residual_block);
  }

  void InitCovariance(BaseMeta::Ptr meta, const Eigen::MatrixXf& cov) {
    size_t new_size = meta->local_id() + meta->size();
    size_t lid = meta->local_id();
    size_t meta_size = meta->size();
    CHECK_EQ(cov.rows(), meta_size);
    if (covariance_.rows() < new_size) ConservativeResize(&covariance_, new_size, new_size);
    covariance_.block(lid, lid, meta_size, meta_size) = cov;
  }

  void PropagateCov(BaseMeta::Ptr src, BaseMeta::Ptr dst, const Eigen::MatrixXf& Jd2s) {
    auto cov_rows = covariance_.rows();
    auto d_lid = dst->local_id(), s_lid = src->local_id();
    covariance_.block(0, d_lid, cov_rows, dst->size()) +=
        covariance_.block(0, s_lid, cov_rows, src->size()) *
            Jd2s.transpose();
    covariance_.block(d_lid, 0, dst->size(), cov_rows) +=
        Jd2s * covariance_.block(s_lid, 0, src->size(), cov_rows);
  }

  void InitSqrtInfoFromCov() {
    Eigen::MatrixXf info = covariance_.inverse();
    sqrt_info_ = Eigen::LLT<Eigen::MatrixXf>(info).matrixL().transpose();
    srif_residual_ = Eigen::VectorXf::Zero(sqrt_info_.rows());
    new_variables_.clear();
  }

  void Reset() {
    srif_problem_->Clear();
    sqrt_info_ = Eigen::MatrixXf();
    covariance_ = Eigen::MatrixXf();
    new_variables_.clear();
    variables_.clear();
  }

  void UpdateCovariance() {
    static common::Timer timer("update cov");
    timer.Tic();
    Eigen::RatrixXf Up =
        sqrt_info_.triangularView<Eigen::Upper>().solve(
            Eigen::MatrixXf::Identity(sqrt_info_.rows(), sqrt_info_.rows()));
    covariance_ = Up.triangularView<Eigen::Upper>() * Up.transpose();
    timer.Toc();
    VLOG(2) << timer;
  }

  void FastUpdateCovariance() {
    static common::Timer timer("update cov");
    size_t idx = 0;
    for (auto& var : variables_) {
      if (var->derived_type == BaseMeta::DerivedType::LANDMARK) {
        idx += var->size();
      } else {
        break;
      }
    }
    size_t sub_size = sqrt_info_.rows() - idx;

    timer.Tic();
    Eigen::RatrixXf Up =
        sqrt_info_.block(idx, idx, sub_size, sub_size).triangularView<Eigen::Upper>().solve(
            Eigen::MatrixXf::Identity(sub_size, sub_size));

    covariance_ = Eigen::MatrixXf::Zero(sqrt_info_.rows(), sqrt_info_.rows());
    covariance_.block(idx, idx, sub_size, sub_size) =
        Up.triangularView<Eigen::Upper>() * Up.transpose();
    timer.Toc();
    VLOG(2) << timer;
  }


  void AdjustSquareInfo(BaseMeta::Ptr var) {
//    return;
    CHECK_EQ(covariance_.rows(), sqrt_info_.rows()) << "cov rows and sqrt rows "
                                                    << covariance_.rows() << " "
                                                    << sqrt_info_.rows();
    auto td_id = var->local_id();
    auto sub_cov = covariance_.block(td_id, td_id, var->size(), var->size());
    float value = sub_cov.cwiseAbs().minCoeff();
    if (value > 1e-6) return;
    float s = sqrt(value * 1e6);
    sqrt_info_.middleRows(td_id, var->size()) *= s;
    VLOG(2) << " AdjustSquareInfo " << s;
  }

  void AddPrior() {
    std::unordered_map<BaseMeta::Ptr, Eigen::MatrixXf> x2phi;
    CHECK(new_variables_.empty()) << "new variable size: " << new_variables_.size();
    for (auto& var : variables_) {
      x2phi[var] = sqrt_info_.middleCols(var->local_id(), var->size());
    }
    SrifResidualBlock::Ptr
        block = std::make_shared<SrifResidualBlock>(x2phi, srif_residual_, "prior");
    srif_problem_->AddResidualBlock(block);
  }

  void AddBigPrior() {
    CHECK(new_variables_.empty()) << "new variable size: " << new_variables_.size();
    std::unordered_map<BaseMeta::Ptr, Eigen::MatrixXf> x2phi;
    x2phi[variables_.front()] = sqrt_info_;
    SrifResidualBlock::Ptr
        block = std::make_shared<SrifResidualBlock>(x2phi, srif_residual_, "big_prior");
    srif_problem_->AddResidualBlock(block);
  }

  void MarginCore(const std::vector<BaseMeta::Ptr>& marg_orders);

  void Solve(bool solve_now, bool only_msckf);

  void SolveSlamUpdate(bool solve_now);

  void InsertPropagateLandmarks(const std::vector<LandmarkMeta::Ptr>& propagate_landmarks) {
    size_t slam_size = GetSlamSize();
    variables_.insert(variables_.begin() + slam_size,
                      propagate_landmarks.begin(),
                      propagate_landmarks.end());
    new_variables_.insert(propagate_landmarks.begin(), propagate_landmarks.end());
  }

  void InitializeSlamLandmarks(
      const std::vector<SlamFeatInitInfo::Ptr>& slam_infos);

 private:
  //////////////////////////////// variables for square root //////////////////
  std::vector<BaseMeta::Ptr> variables_;

  Eigen::MatrixXf sqrt_info_;  // square root of inverse covariance
  Eigen::VectorXf srif_residual_;

  SrifProblem::Ptr srif_problem_;

  Eigen::MatrixXf covariance_;

  std::unordered_set<BaseMeta::Ptr> new_variables_;

  bool enable_solve_log_ = false;

  bool enable_hacky_speedup_ = true;
};
}  // namespace dm::vio

#endif  // VIO_EKF_FILTER_SRIF_FILTER_H_
