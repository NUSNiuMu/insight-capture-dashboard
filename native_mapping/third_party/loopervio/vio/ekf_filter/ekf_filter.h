#ifndef VIO_EKF_FILTER_EKF_FILTER_H_
#define VIO_EKF_FILTER_EKF_FILTER_H_


#include <memory>
#include <unordered_map>
#include <vector>
#include <unordered_set>
#include "Eigen/Eigen"
#include "glog/logging.h"
#include "vio/data_types.h"
#include "common/timer.h"

namespace dm::vio {

struct MsckfUpdateInfo {
  using Ptr = std::shared_ptr<MsckfUpdateInfo>;
  std::vector<BaseMeta::Ptr> Hx_order;
  Eigen::MatrixXd H_x;
  Eigen::VectorXd res;
  Eigen::MatrixXd R;
};

class EKFilter {
 public:
  using Ptr = std::shared_ptr<EKFilter>;

  void EKFPropagation(
      const std::vector<std::shared_ptr<BaseMeta>> &phi_order_new,
      const std::vector<std::shared_ptr<BaseMeta>> &phi_order_old, const Eigen::MatrixXd &phi,
      const Eigen::MatrixXd &Q);

  void EKFUpdate(const std::vector<BaseMeta::Ptr> &Hx_order,
                 const Eigen::MatrixXd &H, const Eigen::VectorXd &res,
                 const Eigen::MatrixXd &R);

  void AugmentNewVariable(BaseMeta::Ptr new_var,
                          const std::unordered_map<BaseMeta::Ptr, Eigen::MatrixXd> &Jnew2olds);

  Eigen::MatrixXd GetMarginalCovariance(
      const std::vector<BaseMeta::Ptr> &variables);

  size_t GetCovarianceSize() const { return covariance_.rows(); }

  void InsertVariable(const BaseMeta::Ptr &data) { variables_.insert(data); }

  const Eigen::MatrixXd covariance() {return covariance_;}

  std::unordered_set<BaseMeta::Ptr> variables() {return variables_;}

  void InitCovariance(BaseMeta::Ptr meta, const Eigen::MatrixXd &cov);

  void InitializeInvertible(BaseMeta::Ptr new_var, const std::vector<BaseMeta::Ptr> &Hx_order,
                            const Eigen::MatrixXd &H_x,
                            const Eigen::MatrixXd &H_f,
                            const Eigen::VectorXd &res, const Eigen::MatrixXd &R);

  void MarginVariable(const BaseMeta::Ptr& data);

 private:
  std::unordered_set<BaseMeta::Ptr> variables_;

  // The cov of imu_meta and frames_, which size is 15+6*n+x
  Eigen::MatrixXd covariance_;
};
}  // namespace dm::vio
#endif  // VIO_EKF_FILTER_EKF_FILTER_H_
